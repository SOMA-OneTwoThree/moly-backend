"""기존 개발 Supabase 전용. 합성 행·설정은 fixture 트랜잭션에서 모두 rollback.

MOLY_CHECKPOINT_TEST_ENV=dev로 부모 작업자가 명시 실행한다. 모델 호출은 없다.
"""
import json
import uuid

from sqlalchemy import text

from app.services import checkpoint, checkpoint_repo as repo, context_retention
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db
from worker import memory_sweep_jobs as sweep


async def seed(session, uid, turns=1):
    ids = []
    for seq in range(1, turns+1):
        for sender, pos in (("user", 1), ("moly", 2)):
            ids.append((await session.execute(text("""
              INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position)
              VALUES(:u,:sender,'safety','Synthetic pressure source',CURRENT_DATE,:seq,:pos)
              RETURNING id
            """), {"u": uid, "sender": sender, "seq": seq, "pos": pos})).scalar_one())
    await session.execute(text("UPDATE chat_contexts SET last_committed_turn_seq=:seq WHERE user_id=:u"),
                          {"u": uid, "seq": turns})
    await session.execute(text("""
      INSERT INTO app_config(key,value) VALUES('context_retention_v2_enabled','true'::jsonb),
        ('context_retention_ready_users',CAST(:cohort AS jsonb))
      ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
    """), {"cohort": json.dumps([str(uid)])})
    return ids


async def target(session, uid):
    return (await session.execute(text("SELECT retention_pressure_through_message_id FROM chat_contexts WHERE user_id=:u"),
                                  {"u": uid})).scalar_one()


async def test_pressure_queues_single_small_complete_turn_and_keeps_active_payload(checkpoint_db):
    session, (uid, _) = checkpoint_db
    ids = await seed(session, uid)
    first = await repo.request_pressure(session, user_id=uid, generation=0)
    assert first.status == "queued" and first.through_message_id == ids[-1]
    payload = (await session.execute(text("SELECT payload FROM async_jobs WHERE id=:j"),
                                    {"j": first.job_id})).scalar_one()
    assert payload["source_message_ids"] == ids and payload["reason"] == "input_pressure"
    assert (await repo.request_pressure(session, user_id=uid, generation=0)).status == "pending"
    assert (await session.execute(text("SELECT payload FROM async_jobs WHERE id=:j"),
                                  {"j": first.job_id})).scalar_one() == payload
    assert await target(session, uid) == ids[-1]
    assert (await session.execute(text("SELECT pending_anchor_message_id FROM chat_contexts WHERE user_id=:u"),
                                  {"u": uid})).scalar_one() is None


async def test_pressure_uses_last_committed_turn_and_rejects_unfinished_or_foreign_boundary(checkpoint_db):
    session, (uid, other) = checkpoint_db
    ids = await seed(session, uid, 2)
    await session.execute(text("UPDATE chat_contexts SET last_committed_turn_seq=1 WHERE user_id=:u"), {"u": uid})
    assert (await repo.request_pressure(session, user_id=uid, generation=0)).through_message_id == ids[1]
    assert (await repo.request_pressure(session, user_id=uid, generation=0, through_id=ids[2])).status == "no_source"
    assert (await repo.request_pressure(session, user_id=other, generation=0, through_id=ids[-1])).status == "not_adopted"
    # The explicit reply is permitted after both new messages flush, before finish_publish.
    explicit = await repo.request_pressure(session, user_id=uid, generation=0, through_id=ids[-1])
    assert explicit.through_message_id == ids[-1] and await target(session, uid) == ids[-1]


async def test_pressure_only_clears_when_published_coverage_reaches_current_goal(checkpoint_db):
    session, (uid, _) = checkpoint_db
    ids = await seed(session, uid, 2)
    await session.execute(repo._SET_PRESSURE, {"user_id": uid, "generation": 0, "through_id": ids[-1]})
    parent = await repo.insert(session, user_id=uid, through_message_id=ids[1], summary="",
        source_hash="a"*64, expected_generation=0, version=checkpoint.RETENTION_SUMMARIZER_VERSION,
        state_json=context_retention.empty_state(), validation_json={"processing_complete": True},
        coverage_from=ids[0])
    assert not await repo.clear_completed_pressure(session, user_id=uid, generation=0, through_id=ids[1])
    assert await target(session, uid) == ids[-1]
    await repo.insert(session, user_id=uid, through_message_id=ids[-1], summary="",
        source_hash="b"*64, expected_generation=0, version=checkpoint.RETENTION_SUMMARIZER_VERSION,
        state_json=context_retention.empty_state(), validation_json={"processing_complete": True},
        previous_id=parent, coverage_from=ids[2])
    assert not await repo.clear_completed_pressure(session, user_id=uid, generation=1, through_id=ids[-1])
    assert await repo.clear_completed_pressure(session, user_id=uid, generation=0, through_id=ids[-1])
    assert await target(session, uid) is None


async def test_generation_and_privacy_epoch_reset_goal_in_same_transaction(checkpoint_db):
    session, (uid, _) = checkpoint_db
    ids = await seed(session, uid)
    await session.execute(repo._SET_PRESSURE, {"user_id": uid, "generation": 0, "through_id": ids[-1]})
    await session.execute(text("UPDATE chat_contexts SET memory_generation=1 WHERE user_id=:u"), {"u": uid})
    assert await target(session, uid) is None
    await session.execute(repo._SET_PRESSURE, {"user_id": uid, "generation": 1, "through_id": ids[-1]})
    await session.execute(text("UPDATE privacy_subject_barriers SET epoch=epoch+1 WHERE user_id=:u"), {"u": uid})
    assert await target(session, uid) is None
    await session.execute(repo._SET_PRESSURE, {"user_id": uid, "generation": 1, "through_id": ids[-1]})
    await session.execute(text("UPDATE privacy_subject_barriers SET state='deleting',operation_id=:op WHERE user_id=:u"),
                          {"u": uid, "op": uuid.uuid4()})
    assert await target(session, uid) is None
    assert (await session.execute(repo._SET_PRESSURE,
        {"user_id": uid, "generation": 1, "through_id": ids[-1]})).first() is None


async def test_cursor_actual_cas_refuses_old_value_or_deleted_row(checkpoint_db):
    session, _ = checkpoint_db
    old, newer, stale = (str(uuid.uuid4()) for _ in range(3))
    await session.execute(text("INSERT INTO app_config(key,value) VALUES('context_retention_resume_cursor',CAST(:v AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"),
                          {"v": json.dumps(old)})
    assert (await session.execute(sweep._RESUME_CURSOR_WRITE,
        {"expected": json.dumps(stale), "next": json.dumps(newer)})).first() is None
    assert (await session.execute(sweep._RESUME_CURSOR_WRITE,
        {"expected": json.dumps(old), "next": json.dumps(newer)})).first() is not None
    await session.execute(text("DELETE FROM app_config WHERE key='context_retention_resume_cursor'"))
    assert (await session.execute(sweep._RESUME_CURSOR_WRITE,
        {"expected": json.dumps(old), "next": json.dumps(newer)})).first() is None
    assert (await session.execute(sweep._RESUME_CURSOR_WRITE,
        {"expected": "null", "next": json.dumps(newer)})).first() is not None
