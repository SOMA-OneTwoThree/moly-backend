"""Global activation applies equally; v5 history only preserves OFF compatibility."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import chat, context_retention as retention, crisis_chat, crisis_guard, memory_pipeline
from tests.test_guard_context_v2_integration_review import setup as setup, Session, request, UID


@pytest.mark.parametrize("guard,enabled,marker", [
    (True, True, False), (False, True, False), (True, False, False),
    (False, False, False), (True, False, True),
])
async def test_global_switches_need_no_ready_list(setup, monkeypatch, guard, enabled, marker):
    _, config = setup
    config.update(crisis_guard_enabled=guard, context_retention_v2_enabled=enabled)
    assert not any("ready_users" in key for key in config)
    decision = AsyncMock(return_value=crisis_guard.DecisionResult("ok", route="normal"))
    monkeypatch.setattr(crisis_chat, "recorded_decision", decision)
    original = chat._context
    context = AsyncMock(wraps=original)
    monkeypatch.setattr(chat, "_context", context)
    session = Session()
    session.get_map["ChatContext"] = SimpleNamespace(anchor_message_id=0,
        memory_generation=0, last_active_at=None, retention_v5_adopted=marker)
    await chat.post_message(session, UID, request(), "global-activation")
    assert decision.await_count == int(guard)
    assert context.await_args.kwargs["retention_enabled"] is (enabled or marker)


@pytest.mark.parametrize("enabled,history", [(True, False), (True, True), (False, True), (False, False)])
@pytest.mark.parametrize("lock", [False, True])
async def test_global_adoption_and_off_compatibility(monkeypatch, enabled, history, lock):
    flag = AsyncMock(return_value=enabled)
    monkeypatch.setattr(retention, "enabled_now", flag)
    scalar = AsyncMock(return_value=history)
    session = SimpleNamespace(scalar=scalar)
    assert await retention.adopted_user(session, user_id=uuid.uuid4(), lock=lock,
        config={"context_retention_v2_enabled": enabled}) is (enabled or history)
    assert scalar.await_count == int(not enabled)
    if lock:
        flag.assert_awaited_once_with(session, lock=True)


@pytest.mark.parametrize("mode,bootstrap,exists", [
    ("v2", "ready", True), ("shadow", "collecting", True), ("legacy", "ready", True),
    ("legacy", "ready", False),
])
async def test_pipeline_initialization_never_certifies_legacy_history(monkeypatch, mode, bootstrap, exists):
    uid = uuid.uuid4()
    state = memory_pipeline.PipelineState(uid, mode, bootstrap, 0, 0, 0, 0, 0, 0, exists=exists)
    ready = memory_pipeline.PipelineState(uid, "v2", "ready", 0, 0, 0, 0, 0, 0)
    load = AsyncMock(side_effect=[state, ready] if not exists else [state])
    monkeypatch.setattr(memory_pipeline, "load", load)
    enroll = AsyncMock(return_value=True)
    advance = AsyncMock()
    events = AsyncMock()
    ingest = AsyncMock()
    monkeypatch.setattr(memory_pipeline, "enroll", enroll)
    monkeypatch.setattr(memory_pipeline, "advance_source", advance)
    monkeypatch.setattr(memory_pipeline, "record_turn_events", events)
    monkeypatch.setattr(memory_pipeline, "enqueue_ingest", ingest)
    await chat._record_memory_v2(object(), uid, turn_seq=1, activity_date=date(2026, 11, 4),
        now=datetime(2026, 11, 4, tzinfo=timezone.utc))
    assert enroll.await_count == int(not exists)
    records = mode != "legacy" or not exists
    assert advance.await_count == int(records)
    assert events.await_count == int(records)
    assert ingest.await_count == int(records and bootstrap == "ready")


def test_sweep_does_not_require_any_per_user_rollout_list():
    from worker import memory_sweep_jobs as sweep
    sql = str(sweep._RETENTION_RESUME_USERS)
    assert "ready_users" not in sql
    assert "latest.through_message_id IS NOT NULL OR EXISTS" not in sql
    assert "retention_pressure_through_message_id" in sql
    assert "state<>'active'" in sql


@pytest.mark.parametrize("field,value", [
    (3, "legacy"), (3, None), (4, "collecting"), (8, 1), (9, True), (10, True),
])
async def test_global_activation_does_not_certify_unprepared_existing_sources(field, value):
    # generation/contract cursors can look complete while pipeline/provenance is not.
    row = [2, 2, 40, "v2", "ready", 4, 40, 4, 0, False, False]
    row[field] = value
    class Result:
        def first(self):
            return row
    session = SimpleNamespace(execute=AsyncMock(return_value=Result()), get=AsyncMock(return_value=None))
    assert not await retention.processing_complete(session, user_id=uuid.uuid4(),
        generation=2, through_id=40)


async def test_legacy_summary_is_never_promoted_to_raw_omission_coverage():
    from app.services import checkpoint, checkpoint_repo
    value = checkpoint.Checkpoint(uuid.uuid4(), 40, "synthetic old summary",
        "conversation-checkpoint-summary-v4", "old-hash")
    session = SimpleNamespace(execute=AsyncMock())
    assert await checkpoint_repo.coverage_for_checkpoint(session, user_id=uuid.uuid4(),
        generation=0, value=value) is None
    session.execute.assert_not_awaited()


async def test_unconverted_legacy_contract_is_not_silently_certified_as_retained():
    from app.services import contract_repo
    class Result:
        def first(self):
            return ([{"kind": "tone", "action": "avoid", "condition": "always"}],)
    session = SimpleNamespace(execute=AsyncMock(return_value=Result()))
    assert await contract_repo.retained_document(session, user_id=uuid.uuid4(), generation=0) == []


async def test_off_switch_keeps_background_precompute_closed(monkeypatch):
    # A production preparation artifact must not assume old workers run while OFF.
    from worker import memory_sweep_jobs as sweep
    monkeypatch.setattr(sweep, "get_sessionmaker", lambda: SessionManager())
    monkeypatch.setattr(sweep, "_retention_enabled", AsyncMock(return_value=False))
    class SessionManager:
        def __call__(self):
            return self
        async def __aenter__(self):
            return SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("no preparation while off")))
        async def __aexit__(self, *args):
            pass
    result = await sweep._resume_retention_checkpoints()
    assert result.candidates == result.enqueued == 0
