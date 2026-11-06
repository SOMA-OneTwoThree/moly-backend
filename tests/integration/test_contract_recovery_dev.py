"""Dev DB, outer rollback: the real range-closed marking SQL on generated fixture owners only.

Runs only with MOLY_CHECKPOINT_TEST_ENV=dev. No provider/Slack call and no runnable job.
"""
import json
import uuid

from sqlalchemy import text

from app.services import contract_recovery, jobs
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db

CURSOR = 1000


async def _dead(session, uid, *, through, generation=0, prep=False, result_code=None, redacted=False):
    payload = {"retention_enabled": True, "memory_generation": generation,
               "through_message_id": through, "after_message_id": 0}
    if prep:
        payload["retention_preparation_id"] = str(uuid.uuid4())
    if redacted:
        payload = {}  # 운영 스크럽과 같은 모양(payload='{}', payload_redacted_at 기록)
    return (await session.execute(text(
        "INSERT INTO async_jobs(queue,job_type,user_id,dedup_key,payload,state,max_attempts,attempt,"
        " finished_at,result_code,last_error_code,payload_hash,payload_redacted_at)"
        " VALUES('memory','contract_compile',:u,:k,CAST(:p AS jsonb),'dead',3,3,now(),:rc,"
        " 'contract_retention_evidence','h',CASE WHEN :red THEN now() END) RETURNING id"),
        {"u": uid, "k": f"contract-recovery-test:{uuid.uuid4()}", "p": json.dumps(payload),
         "rc": result_code, "red": redacted})).scalar_one()


async def _fixture_dead_count(session, uid):
    statement = text(str(jobs._STATS_SQL).replace(
        "GROUP BY queue, state", "AND j.user_id=:u\nGROUP BY queue, state"))
    rows = (await session.execute(statement, {"u": uid})).mappings().all()
    return sum(int(r["n"]) for r in rows if r["state"] == "dead")


async def test_marks_only_closed_ranges_and_stats_skip_them(checkpoint_db):
    session, users = checkpoint_db
    uid = users[0]
    await session.execute(text(
        "UPDATE chat_contexts SET memory_generation=0, contract_generation=0,"
        " contract_processed_through_message_id=:c WHERE user_id=:u"), {"c": CURSOR, "u": uid})
    cases = {
        "covered": await _dead(session, uid, through=CURSOR),
        "ahead": await _dead(session, uid, through=CURSOR + 1),
        "zero": await _dead(session, uid, through=0),
        "not_a_number": await _dead(session, uid, through="1x"),
        "negative": await _dead(session, uid, through=-5),
        "other_generation": await _dead(session, uid, through=CURSOR, generation=1),
        "preparation": await _dead(session, uid, through=CURSOR, prep=True),
        "terminal_code": await _dead(session, uid, through=CURSOR, result_code="contract_retention_schema"),
        "redacted": await _dead(session, uid, through=CURSOR, redacted=True),
    }
    assert await _fixture_dead_count(session, uid) == len(cases)

    await contract_recovery.mark_closed(session)
    codes = dict((await session.execute(text(
        "SELECT id, result_code FROM async_jobs WHERE id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": list(cases.values())})).all())
    assert codes[cases["covered"]] == contract_recovery.RANGE_CLOSED
    assert codes[cases["terminal_code"]] == "contract_retention_schema"
    assert all(codes[cases[name]] is None for name in
               ("ahead", "zero", "not_a_number", "negative", "other_generation", "preparation", "redacted"))
    states = set((await session.execute(text(
        "SELECT state FROM async_jobs WHERE id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": list(cases.values())})).scalars())
    assert states == {"dead"}  # 실패 기록은 그대로 둔다.
    assert await _fixture_dead_count(session, uid) == len(cases) - 1

    # 다시 돌려도 바뀌는 것이 없다.
    await contract_recovery.mark_closed(session)
    assert await _fixture_dead_count(session, uid) == len(cases) - 1


async def test_does_not_mark_when_contract_generation_lags_memory_generation(checkpoint_db):
    session, users = checkpoint_db
    uid = users[1]
    await session.execute(text(
        "UPDATE chat_contexts SET memory_generation=1, contract_generation=0,"
        " contract_processed_through_message_id=:c WHERE user_id=:u"), {"c": CURSOR, "u": uid})
    job = await _dead(session, uid, through=CURSOR, generation=1)
    await contract_recovery.mark_closed(session)
    code = (await session.execute(text("SELECT result_code FROM async_jobs WHERE id=:i"), {"i": job})).scalar_one()
    assert code is None
    assert await _fixture_dead_count(session, uid) == 1
