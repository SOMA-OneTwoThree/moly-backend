"""Existing dev Supabase only; synthetic scheduling rows always roll back."""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from app.services import checkpoint, checkpoint_repo, context_retention
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db
from worker import memory_sweep_jobs as sweep


async def seed_plan(session, uid):
    ids = []
    for index in range(4):
        ids.append((await session.execute(text("""
          INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position)
          VALUES(:u,'user','normal',:body,CURRENT_DATE,:seq,1) RETURNING id
        """), {"u": uid, "body": "Synthetic checkpoint source " + "x"*1200,
                 "seq": index+1})).scalar_one())
    sources = await checkpoint_repo.load_range(session, uid, after_id=0,
        through_id=ids[-1], include_excluded=True)
    plan = checkpoint.plan(sources, previous=None, keep_from_message_id=ids[2],
        memory_generation=0, reset_triggered=True, version=checkpoint.RETENTION_SUMMARIZER_VERSION)
    assert plan is not None
    return plan, ids


async def terminal(session, uid, payload, *, state="succeeded", result="checkpoint_disabled"):
    identity = uuid.uuid4()
    await session.execute(text("""
      INSERT INTO async_jobs(id,queue,job_type,user_id,dedup_key,payload,state,max_attempts,
                             result_code,finished_at)
      VALUES(:j,'content','conversation_checkpoint',:u,:dedup,CAST(:payload AS jsonb),:state,3,
             :result,clock_timestamp())
    """), {"j": identity, "u": uid, "dedup": f"synthetic-sweep:{identity}",
             "payload": json.dumps(payload), "state": state, "result": result})
    return identity


def criteria(uid, plan):
    return {"user_id": uid, "generation": 0, "source_hash": plan.source_hash,
            "through_id": plan.through_message_id, "previous_id": ""}


async def selected(session, uid, plan):
    return (await session.execute(sweep._RETENTION_UNPUBLISHED_SUCCESS, criteria(uid, plan))).first()


async def test_actual_sql_resumes_latest_payload_and_repeated_off_on_without_history_changes(checkpoint_db):
    session, (uid, _) = checkpoint_db
    plan, _ = await seed_plan(session, uid)
    payload = {**plan.payload(), "progress_state": context_retention.empty_state(),
               "progress_next_chunk": 3, "progress_manifest": "a"*64}
    original = await terminal(session, uid, payload)
    expected_parent = original
    for _ in range(3):
        row = await selected(session, uid, plan)
        assert row[0] == expected_parent and row[1] == payload
        resumed = await sweep._resume_unpublished_success(session, user_id=uid,
            generation=0, plan=plan, previous=None)
        assert resumed is not None
        stored = (await session.execute(text("SELECT dedup_key,payload,state FROM async_jobs WHERE id=:j"),
            {"j": resumed})).one()
        assert stored[0] == f"checkpoint-resume:{expected_parent}" and stored[1] == payload
        assert stored[2] == "ready"
        assert await selected(session, uid, plan) is None  # active successor blocks another sweep
        # Only our synthetic successor simulates finishing while the switch is off.
        await session.execute(text("""
          UPDATE async_jobs SET state='succeeded',result_code='checkpoint_disabled',
            finished_at=clock_timestamp() WHERE id=:j AND user_id=:u AND state='ready'
        """), {"j": resumed, "u": uid})
        expected_parent = resumed
    original_row = (await session.execute(text("SELECT state,payload FROM async_jobs WHERE id=:j"),
        {"j": original})).one()
    assert original_row[0] == "succeeded" and original_row[1] == payload


async def test_actual_sql_excludes_published_coverage_active_work_and_changed_generation(checkpoint_db):
    session, (uid, other) = checkpoint_db
    plan, ids = await seed_plan(session, uid)
    own = await terminal(session, uid, plan.payload())
    await terminal(session, other, plan.payload())
    assert (await selected(session, uid, plan))[0] == own
    async with session.begin_nested() as savepoint:
        await terminal(session, uid, plan.payload(), state="ready")
        assert await selected(session, uid, plan) is None
        await savepoint.rollback()
    async with session.begin_nested() as savepoint:
        await checkpoint_repo.insert(session, user_id=uid, through_message_id=plan.through_message_id,
            summary="", source_hash=plan.source_hash, expected_generation=0,
            version=checkpoint.RETENTION_SUMMARIZER_VERSION,
            state_json=context_retention.empty_state(), validation_json={"processing_complete": True})
        assert await selected(session, uid, plan) is None
        await savepoint.rollback()
    async with session.begin_nested() as savepoint:
        await session.execute(text("UPDATE chat_contexts SET memory_generation=1 WHERE user_id=:u"), {"u": uid})
        assert await selected(session, uid, plan) is None
        await savepoint.rollback()
    async with session.begin_nested() as savepoint:
        # A newly published earlier boundary changes the expected parent as well.
        await checkpoint_repo.insert(session, user_id=uid, through_message_id=ids[0],
            summary="", source_hash="b"*64, expected_generation=0,
            version=checkpoint.RETENTION_SUMMARIZER_VERSION,
            state_json=context_retention.empty_state(), validation_json={"processing_complete": True})
        assert await selected(session, uid, plan) is None
        await savepoint.rollback()
    assert (await selected(session, uid, plan))[0] == own


async def test_actual_sql_does_not_reset_newer_dead_retry_budget(checkpoint_db):
    session, (uid, _) = checkpoint_db
    plan, _ = await seed_plan(session, uid)
    await terminal(session, uid, plan.payload(), result="checkpoint_progress_page")
    assert await selected(session, uid, plan) is not None
    dead = await terminal(session, uid, plan.payload(), state="dead", result="summary_llm_failed")
    assert await selected(session, uid, plan) is None
    assert (await session.execute(text("SELECT state FROM async_jobs WHERE id=:j"), {"j": dead})).scalar_one() == "dead"


async def test_actual_correction_scan_pages_short_sources_and_finds_late_correction(checkpoint_db):
    session, (uid, _) = checkpoint_db
    await session.execute(text("""
      INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position)
      SELECT :u,'user','normal','hi',CURRENT_DATE,seq,1 FROM generate_series(1,500) seq
    """), {"u": uid})
    assert not await context_retention.pending_correction(session, user_id=uid,
        generation=0, current_text="hello")
    await session.execute(text("""
      INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position)
      VALUES(:u,'user','normal','금요일 아니고 토요일',CURRENT_DATE,501,1)
    """), {"u": uid})
    assert await context_retention.pending_correction(session, user_id=uid,
        generation=0, current_text="hello")


async def test_contract_off_on_resumes_unprocessed_partial_and_stops_at_processed_cursor(checkpoint_db):
    session, (uid, _) = checkpoint_db
    plan, _ = await seed_plan(session, uid)
    target = plan.through_message_id
    original = await context_retention.enqueue_contract_processing(session, user_id=uid,
        generation=0, through_id=target)
    assert original is not None
    assert await context_retention.enqueue_contract_processing(session, user_id=uid,
        generation=0, through_id=target) is None  # active original is not duplicated
    # A synthetic partial successor survives an off period without advancing coverage.
    await session.execute(text("""
      UPDATE async_jobs SET state='succeeded',result_code='ok',finished_at=clock_timestamp()
      WHERE id=:j AND user_id=:u
    """), {"j": original, "u": uid})
    payload = {"retention_enabled": True, "memory_generation": 0, "through_message_id": target,
               "after_message_id": 0, "partial_message_id": plan.source_message_ids[0],
               "partial_offset": 128, "partial_source_hash": "a"*64, "partial_items": [],
               "partial_drafts": [], "partial_document_hash": "b"*64}
    partial = uuid.uuid4()
    await session.execute(text("""
      INSERT INTO async_jobs(id,queue,job_type,user_id,dedup_key,payload,state,max_attempts,result_code,finished_at)
      VALUES(:j,'content','contract_compile',:u,:key,CAST(:payload AS jsonb),'cancelled',3,
             'contract_retention_disabled',clock_timestamp())
    """), {"j": partial, "u": uid, "key": f"contract-retention:{uid}:0:0:{target}:source:synthetic",
             "payload": json.dumps(payload)})
    previous = partial
    for _ in range(2):
        resumed = await context_retention.enqueue_contract_processing(session, user_id=uid,
            generation=0, through_id=target)
        row = (await session.execute(text("SELECT dedup_key,payload FROM async_jobs WHERE id=:j"),
            {"j": resumed})).one()
        assert row[0] == f"contract-retention-resume:{previous}" and row[1] == payload
        assert await context_retention.enqueue_contract_processing(session, user_id=uid,
            generation=0, through_id=target) is None
        await session.execute(text("""
          UPDATE async_jobs SET state='succeeded',result_code='ok',finished_at=clock_timestamp()
          WHERE id=:j AND user_id=:u
        """), {"j": resumed, "u": uid})
        previous = resumed
    await session.execute(text("""
      UPDATE chat_contexts SET contract_generation=0,contract_processed_through_message_id=:target WHERE user_id=:u
    """), {"u": uid, "target": target})
    assert await context_retention.enqueue_contract_processing(session, user_id=uid,
        generation=0, through_id=target) is None
    assert (await session.execute(text("SELECT state FROM async_jobs WHERE id=:j"),
        {"j": original})).scalar_one() == "succeeded"


@pytest.mark.parametrize("state,expected", [("dead", False), ("ready", False),
                                             ("succeeded", True), ("cancelled", True)])
async def test_contract_suffix_only_history_cannot_reset_retry_budget(checkpoint_db, state, expected):
    session, (uid, _) = checkpoint_db
    plan, _ = await seed_plan(session, uid)
    target, identity = plan.through_message_id, uuid.uuid4()
    base = f"contract-retention:{uid}:0:0:{target}"
    payload = {"retention_enabled": True, "memory_generation": 0, "through_message_id": target}
    await session.execute(text("""
      INSERT INTO async_jobs(id,queue,job_type,user_id,dedup_key,payload,state,max_attempts,result_code,finished_at)
      VALUES(:j,'content','contract_compile',:u,:key,CAST(:payload AS jsonb),:state,3,
             :result,clock_timestamp())
    """), {"j": identity, "u": uid, "key": f"{base}:after:synthetic-parent",
             "payload": json.dumps(payload), "state": state,
             "result": "summary_llm_failed" if state == "dead" else "contract_retention_disabled"})
    resumed = await context_retention.enqueue_contract_processing(session, user_id=uid,
        generation=0, through_id=target)
    assert (resumed is not None) is expected
    assert (await session.execute(text("SELECT count(*) FROM async_jobs WHERE user_id=:u AND dedup_key=:key"),
        {"u": uid, "key": base})).scalar_one() == 0
    assert (await session.execute(text("SELECT state FROM async_jobs WHERE id=:j"),
        {"j": identity})).scalar_one() == state
    if resumed is not None:
        assert (await session.execute(text("SELECT dedup_key FROM async_jobs WHERE id=:j"),
            {"j": resumed})).scalar_one() == f"contract-retention-resume:{identity}"
