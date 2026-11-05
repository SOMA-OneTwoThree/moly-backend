"""Actual development SQL only; every synthetic row is outer-rolled-back.

Uses the existing guarded dev fixture. No model/provider calls or global flags,
no runnable jobs, and no committed Auth/profile/source/artifact changes.
"""
from dataclasses import replace
import hashlib
import json
import uuid

import pytest
from sqlalchemy import text

from app.services import checkpoint, checkpoint_repo, context_compat, context_retention
from app.services.retention_preparation_context import Binding, use_binding
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db


async def message(session, uid, *, turn, sender="user", kind="normal"):
    return await session.scalar(text("""INSERT INTO messages
      (user_id,sender,kind,content,activity_date,turn_seq,turn_position)
      VALUES(:u,:sender,:kind,'Synthetic compatibility source',CURRENT_DATE,:turn,:position)
      RETURNING id"""), {"u": uid, "sender": sender, "kind": kind, "turn": turn,
                          "position": 1 if sender == "user" else 2})


async def candidate(session, uid, turn):
    await session.execute(text("""INSERT INTO mem0_ingest_candidates
      (user_id,turn_seq,candidate_hash,schema_version,extractor_version,normalizer_version,
       provider_memory_id,candidate_text,status)
      VALUES(:u,:turn,:hash,'synthetic','synthetic','synthetic',:pid,'Synthetic candidate','planned')"""),
      {"u": uid, "turn": turn, "hash": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
       "pid": uuid.uuid4()})


async def registry(session, uid, turn, status):
    await session.execute(text("""INSERT INTO mem0_memory_registry
      (user_id,provider,collection_version,provider_memory_id,source_turn_seq,content_hash,
       schema_version,semantic_status,canonical_text)
      VALUES(:u,'synthetic','v2',:pid,:turn,:hash,'synthetic',:status,NULL)"""),
      {"u": uid, "turn": turn, "pid": uuid.uuid4(),
       "hash": hashlib.sha256(uuid.uuid4().bytes).hexdigest(), "status": status})


async def seed(session, uid, *, suffix_null=False, policy_only=False):
    old = await message(session, uid, turn=None)
    await session.execute(text("""UPDATE chat_contexts SET anchor_message_id=:a,
      contract_generation=memory_generation,contract_processed_through_message_id=0
      WHERE user_id=:u"""), {"u": uid, "a": old+1})
    baseline = await context_compat.initialize(session, user_id=uid, generation=0, privacy_epoch=0,
        source_after=old, legacy_anchor=old+1, legacy_checkpoint=None, locale="en")
    first = await message(session, uid, turn=None if suffix_null else 10,
                          kind="safety" if policy_only else "normal")
    last = await message(session, uid, turn=10, sender="moly",
                         kind="safety" if policy_only else "normal")
    await session.execute(text("""INSERT INTO memory_pipeline_states
      (user_id,source_through_turn_seq,ingest_through_turn_seq,consolidated_through_turn_seq,
       mode,bootstrap_status) VALUES(:u,10,10,10,'v2','ready')
      ON CONFLICT(user_id) DO UPDATE SET source_through_turn_seq=10,ingest_through_turn_seq=10,
        consolidated_through_turn_seq=10,mode='v2',bootstrap_status='ready'"""), {"u": uid})
    await session.execute(text("""UPDATE chat_contexts SET contract_generation=memory_generation,
      contract_processed_through_message_id=:last WHERE user_id=:u"""), {"u": uid, "last": last})
    await candidate(session, uid, 1)
    await registry(session, uid, 1, "pending")
    await registry(session, uid, 1, "active")
    return baseline, first, last


@pytest.mark.parametrize("suffix_failure", [None, "candidate", "pending", "canonical_null", "unsequenced"])
async def test_real_suffix_completion_ignores_old_pending_but_blocks_new(checkpoint_db, suffix_failure):
    session, (uid, _) = checkpoint_db
    baseline, _first, last = await seed(session, uid, suffix_null=suffix_failure == "unsequenced")
    if suffix_failure == "candidate":
        await candidate(session, uid, 10)
    if suffix_failure in {"pending", "canonical_null"}:
        await registry(session, uid, 10, "pending" if suffix_failure == "pending" else "active")
    assert await context_compat.matches_current(session, baseline)
    result = await context_retention.processing_complete(session, user_id=uid, generation=0, through_id=last)
    # canonical_text가 빈 active 기억은 배포 전 기억이다. 변환하지 않고 쓰므로 게시를 막지 않는다.
    assert result is (suffix_failure in {None, "canonical_null"})
    assert await session.scalar(text("SELECT count(*) FROM mem0_memory_registry WHERE user_id=:u"),
                                {"u": uid}) >= 2


async def test_real_policy_only_suffix_does_not_require_old_fact_reprocessing(checkpoint_db):
    session, (uid, _) = checkpoint_db
    _baseline, _first, last = await seed(session, uid, policy_only=True)
    await session.execute(text("UPDATE memory_pipeline_states SET mode='legacy',bootstrap_status='legacy' WHERE user_id=:u"),
                          {"u": uid})
    assert await context_retention.processing_complete(session, user_id=uid, generation=0, through_id=last)


async def test_real_incremental_coverage_never_covers_baseline_prefix(checkpoint_db):
    session, (uid, _) = checkpoint_db
    baseline, first, last = await seed(session, uid)
    sources = await checkpoint_repo.load_range(session, uid, after_id=baseline.source_after,
                                               through_id=last, include_excluded=True)
    state = {**context_retention.empty_state(), "sequence": 1}
    digest = checkpoint.source_hash(previous=None, messages=sources,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION, baseline_binding=baseline.payload())
    metadata = {"processing_complete": True, "baseline_binding": baseline.payload(),
                "source_message_ids": [first, last]}
    identity = await checkpoint_repo.insert(session, user_id=uid, through_message_id=last,
        summary="Synthetic incremental summary", source_hash=digest, expected_generation=0,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION, state_json=state,
        validation_json=metadata, coverage_from=first)
    assert identity is not None
    current = await checkpoint_repo.load_latest(session, uid, retention_enabled=True)
    coverage = await checkpoint_repo.coverage_for_checkpoint(session, user_id=uid, generation=0, value=current)
    assert coverage.after_message_id == baseline.source_after
    assert coverage.through_message_id == last
    assert await checkpoint_repo.load_latest(session, uid, retention_enabled=False) is None


async def test_real_final_insert_cas_rejects_stale_cached_baseline(checkpoint_db):
    from app.models.chat_context import ChatContext
    session, (uid, _) = checkpoint_db
    baseline, first, last = await seed(session, uid)
    held_context = await session.get(ChatContext, uid)
    assert await context_compat.load(session, uid, 0) == baseline
    changed = replace(baseline, baseline_id=str(uuid.uuid4()))
    await session.execute(text("UPDATE chat_contexts SET retention_compat_json=CAST(:b AS jsonb) WHERE user_id=:u"),
                          {"u": uid, "b": json.dumps(changed.payload())})
    # Deliberately retain the old ORM object: final SQL must not trust that cache.
    assert held_context.retention_compat_json == baseline.payload()
    assert await context_compat.load(session, uid, 0) == baseline
    assert not await context_compat.matches_current(session, baseline)
    assert await checkpoint_repo.insert(session, user_id=uid, through_message_id=last,
        summary="Synthetic stale summary", source_hash="a"*64, expected_generation=0,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION,
        state_json=context_retention.empty_state(),
        validation_json={"baseline_binding": baseline.payload(), "processing_complete": True},
        coverage_from=first) is None
    assert await session.scalar(text("SELECT count(*) FROM conversation_checkpoints WHERE user_id=:u"),
                                {"u": uid}) == 0


async def test_real_preparation_namespace_keeps_its_separate_unbound_chain(checkpoint_db):
    session, (uid, _) = checkpoint_db
    _baseline, first, last = await seed(session, uid)
    preparation = uuid.uuid4()
    await session.execute(text("""INSERT INTO retention_preparations(id,state,code_sha,manifest_json)
      VALUES(:p,'building',:sha,'{}'::jsonb)"""), {"p": preparation, "sha": "a"*40})
    with use_binding(Binding(preparation, True)):
        identity = await checkpoint_repo.insert(session, user_id=uid, through_message_id=last,
            summary="Synthetic private summary", source_hash="b"*64, expected_generation=0,
            version=checkpoint.RETENTION_SUMMARIZER_VERSION,
            state_json=context_retention.empty_state(), validation_json={"processing_complete": True},
            coverage_from=first)
        assert identity is not None
        current = await checkpoint_repo.load_latest(session, uid, retention_enabled=True)
        assert current.id == identity
    assert await checkpoint_repo.load_latest(session, uid, retention_enabled=True) is None
    assert await session.scalar(text("SELECT count(*) FROM async_jobs WHERE user_id=:u"), {"u": uid}) == 0
