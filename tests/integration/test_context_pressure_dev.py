"""기존 개발 Supabase 전용. 합성 행·설정은 fixture 트랜잭션에서 모두 rollback.

MOLY_CHECKPOINT_TEST_ENV=dev로 부모 작업자가 명시 실행한다. 모델 호출은 없다.
"""
import json
import uuid

from sqlalchemy import text

from app.services import checkpoint, checkpoint_repo as repo, context_retention, memory_pipeline
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


async def seed_wake_dependency(session, uid):
    ids = await seed(session, uid, turns=2)
    await session.execute(text("UPDATE messages SET kind='normal' WHERE user_id=:u"), {"u": uid})
    await session.execute(text("""
      INSERT INTO memory_pipeline_states(user_id,mode,bootstrap_status,source_through_turn_seq,
        ingest_through_turn_seq,consolidated_through_turn_seq,privacy_epoch,repair_generation)
      VALUES(:u,'v2','ready',2,0,0,0,7)
      ON CONFLICT(user_id) DO UPDATE SET mode='v2',bootstrap_status='ready',
        source_through_turn_seq=2,ingest_through_turn_seq=0,consolidated_through_turn_seq=0,
        privacy_epoch=0,repair_generation=7
    """), {"u": uid})
    job_id = (await session.execute(text("""
      INSERT INTO async_jobs(queue,job_type,user_id,dedup_key,payload,state,attempt,max_attempts,
        available_at,expires_at,payload_expires_at,payload_hash)
      VALUES('memory','mem0_ingest',:u,:dedup,CAST(:payload AS jsonb),'ready',0,3,
        now()+interval '180 seconds',now()+interval '1 day',now()+interval '1 day',:hash)
      RETURNING id
    """), {"u": uid, "dedup": memory_pipeline.ingest_dedup_key(uid, 0, generation=7),
        "payload": json.dumps({"turn_seq": 1, "privacy_epoch": 0, "synthetic_marker": "preserve"}),
        "hash": "a"*64})).scalar_one()
    return ids, job_id


async def wake_job_snapshot(session, job_id):
    return dict((await session.execute(text("SELECT * FROM async_jobs WHERE id=:j"),
                                      {"j": job_id})).mappings().one())


async def test_checkpoint_wake_changes_only_exact_dependency_available_at(checkpoint_db):
    session, (uid, other) = checkpoint_db
    ids, identity = await seed_wake_dependency(session, uid)
    _, foreign = await seed_wake_dependency(session, other)
    # Both fixtures must remain explicitly adopted after the second setup.
    await session.execute(text("UPDATE app_config SET value=CAST(:cohort AS jsonb) WHERE key='context_retention_ready_users'"),
                          {"cohort": json.dumps([str(uid), str(other)])})
    before, unrelated = await wake_job_snapshot(session, identity), await wake_job_snapshot(session, foreign)
    assert await repo.wake_checkpoint_ingest(session, user_id=uid, generation=0, through_id=ids[-1])
    after = await wake_job_snapshot(session, identity)
    now = (await session.execute(text("SELECT now()"))).scalar_one()
    assert before["available_at"] > now and after["available_at"] == now
    assert {key: value for key, value in before.items() if key != "available_at"} == {
        key: value for key, value in after.items() if key != "available_at"}
    assert await wake_job_snapshot(session, foreign) == unrelated
    assert not await repo.wake_checkpoint_ingest(session, user_id=uid, generation=0, through_id=ids[-1])


async def test_checkpoint_wake_preserves_backoff_claimed_masked_expired_and_nonexact_jobs(checkpoint_db):
    session, (uid, _) = checkpoint_db
    ids, identity = await seed_wake_dependency(session, uid)
    changes = (
        "attempt=1",  # First failure's backoff must remain untouched.
        "state='running',attempt=1,lease_owner='synthetic',lease_token=gen_random_uuid(),lease_until=now()+interval '1 minute'",
        "state='dead',attempt=3",
        "payload_redacted_at=now()",
        "payload_expires_at=now()-interval '1 second'",
        "expires_at=now()-interval '1 second'",
        "dedup_key=dedup_key||'-synthetic-other-plan'",
        "payload=payload||jsonb_build_object('turn_seq',99)",
        "payload=payload||jsonb_build_object('privacy_epoch',1)",
        "queue='content'",
    )
    for change in changes:
        async with session.begin_nested() as savepoint:
            # Fragments are the static synthetic cases above; no user-provided SQL.
            await session.execute(text(f"UPDATE async_jobs SET {change} WHERE id=:j"), {"j": identity})
            before = await wake_job_snapshot(session, identity)
            assert not await repo.wake_checkpoint_ingest(session, user_id=uid, generation=0,
                                                        through_id=ids[-1]), change
            assert await wake_job_snapshot(session, identity) == before
            await savepoint.rollback()


async def test_checkpoint_wake_rejects_changed_generation_pipeline_and_privacy(checkpoint_db):
    session, (uid, _) = checkpoint_db
    ids, identity = await seed_wake_dependency(session, uid)
    changes = (
        "UPDATE chat_contexts SET memory_generation=1 WHERE user_id=:u",
        "UPDATE memory_pipeline_states SET repair_generation=8 WHERE user_id=:u",
        "UPDATE memory_pipeline_states SET privacy_epoch=1 WHERE user_id=:u",
        "UPDATE privacy_subject_barriers SET epoch=1 WHERE user_id=:u",
        "UPDATE privacy_subject_barriers SET state='deleting',operation_id=gen_random_uuid() WHERE user_id=:u",
        "UPDATE memory_pipeline_states SET mode='shadow' WHERE user_id=:u",
        "UPDATE memory_pipeline_states SET bootstrap_status='collecting' WHERE user_id=:u",
        "UPDATE memory_pipeline_states SET source_through_turn_seq=0 WHERE user_id=:u",
    )
    for change in changes:
        async with session.begin_nested() as savepoint:
            await session.execute(text(change), {"u": uid})
            before = await wake_job_snapshot(session, identity)
            assert not await repo.wake_checkpoint_ingest(session, user_id=uid, generation=0,
                                                        through_id=ids[-1]), change
            assert await wake_job_snapshot(session, identity) == before
            await savepoint.rollback()
    before = await wake_job_snapshot(session, identity)
    assert not await repo.wake_checkpoint_ingest(session, user_id=uid, generation=0,
                                                through_id=ids[0]-1)
    assert await wake_job_snapshot(session, identity) == before
