"""Checkpoint dependencies skip idle delay, never retry/backoff/lease fences.

Fake sessions/providers only. Actual SQL behavior requires the separately
authorized development DB integration run.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import checkpoint, checkpoint_repo as repo, context_retention, memory_pipeline
from worker import checkpoint_jobs as worker


class Result:
    def __init__(self, row=None):
        self.row = row

    def first(self):
        return self.row


@pytest.fixture
def wake(monkeypatch):
    state = SimpleNamespace(mode="v2", bootstrap_status="ready", ingest_through_turn_seq=0,
                            source_through_turn_seq=4, repair_generation=7, privacy_epoch=3)
    enabled = AsyncMock(return_value=True)
    adopted = AsyncMock(return_value=True)
    monkeypatch.setattr(repo.settings, "privacy_barrier_mode", "compat")
    monkeypatch.setattr(context_retention, "enabled_now", enabled)
    monkeypatch.setattr(context_retention, "adopted_user", adopted)
    monkeypatch.setattr(memory_pipeline, "load", AsyncMock(return_value=state))
    captured = []

    class Session:
        async def execute(self, sql, params):
            captured.append((sql, params))
            return Result((uuid.uuid4(),))

        async def commit(self):
            raise AssertionError("repository must not commit")

    return SimpleNamespace(state=state, enabled=enabled, adopted=adopted,
                           session=Session(), captured=captured)


@pytest.mark.parametrize("repair_generation", [0, 7])
async def test_exact_pipeline_dedup_and_epochs_are_bound_without_changing_attempt_budget(wake, repair_generation):
    uid = uuid.uuid4()
    wake.state.repair_generation = repair_generation
    assert await repo.wake_checkpoint_ingest(wake.session, user_id=uid, generation=2, through_id=8)
    sql, params = wake.captured[0]
    assert sql is repo._WAKE_CHECKPOINT_INGEST
    assert params == {
        "user_id": uid, "generation": 2, "through_id": 8, "cursor": 0,
        "privacy_epoch": 3, "repair_generation": repair_generation,
        "allow_missing_barrier": True,
        "dedup_key": memory_pipeline.ingest_dedup_key(uid, 0, generation=repair_generation),
    }
    assert wake.enabled.await_args.kwargs["lock"] is True
    assert wake.adopted.await_args.kwargs == {"user_id": uid, "lock": True}


@pytest.mark.parametrize("mode,allow_missing", [("compat", True), ("enforced", False)])
async def test_missing_barrier_policy_matches_worker_authorization(wake, monkeypatch, mode, allow_missing):
    monkeypatch.setattr(repo.settings, "privacy_barrier_mode", mode)
    await repo.wake_checkpoint_ingest(wake.session, user_id=uuid.uuid4(), generation=2, through_id=8)
    assert wake.captured[0][1]["allow_missing_barrier"] is allow_missing


@pytest.mark.parametrize("condition", ["disabled", "not_adopted", "legacy", "bootstrap", "caught_up"])
async def test_noncurrent_or_disabled_pipeline_never_touches_a_job(wake, condition):
    if condition == "disabled":
        wake.enabled.return_value = False
    elif condition == "not_adopted":
        wake.adopted.return_value = False
    elif condition == "legacy":
        wake.state.mode = "legacy"
    elif condition == "bootstrap":
        wake.state.bootstrap_status = "collecting"
    else:
        wake.state.ingest_through_turn_seq = wake.state.source_through_turn_seq
    assert not await repo.wake_checkpoint_ingest(wake.session, user_id=uuid.uuid4(), generation=2, through_id=8)
    assert not wake.captured


def test_candidate_sql_only_wakes_first_ready_exact_dependency_and_skips_locked_rows():
    sql = str(repo._WAKE_CHECKPOINT_INGEST)
    required = [
        "j.user_id=:user_id", "cc.memory_generation=:generation",
        "p.mode='v2' AND p.bootstrap_status='ready'", "p.privacy_epoch=:privacy_epoch",
        "p.repair_generation=:repair_generation", "p.ingest_through_turn_seq=:cursor",
        "j.dedup_key=:dedup_key", "j.payload->'privacy_epoch'=to_jsonb(p.privacy_epoch)",
        "j.state='ready' AND j.attempt=0", "j.attempt<j.max_attempts",
        "j.lease_owner IS NULL", "j.lease_token IS NULL", "j.lease_until IS NULL",
        "j.payload_redacted_at IS NULL", "b.state='active' AND b.epoch=p.privacy_epoch",
        "LEFT JOIN privacy_subject_barriers b ON b.user_id=j.user_id",
        "b.user_id IS NULL AND :allow_missing_barrier", "j.available_at>now()",
        "j.payload_expires_at IS NULL OR j.payload_expires_at>now()",
        "m.kind='normal' AND m.id<=:through_id", "m.turn_seq>p.ingest_through_turn_seq",
        "m.turn_seq<=p.source_through_turn_seq", "j.payload->'turn_seq'=to_jsonb(m.turn_seq)",
        "FOR UPDATE OF j SKIP LOCKED LIMIT 1",
    ]
    assert all(value in sql for value in required)
    mutation = sql.split("UPDATE async_jobs j SET", 1)[1].split("FROM candidate", 1)[0].strip()
    assert mutation == "available_at=LEAST(j.available_at,now())"


async def test_claimed_or_unavailable_exact_candidate_returns_false_without_new_work(wake):
    wake.session.execute = AsyncMock(return_value=Result())
    assert not await repo.wake_checkpoint_ingest(wake.session, user_id=uuid.uuid4(), generation=2, through_id=8)
    wake.session.execute.assert_awaited_once()


async def test_handler_commits_contract_reservation_before_separate_dependency_transaction(monkeypatch):
    events, sessions = [], []

    class Session:
        def __init__(self):
            self.name = "read" if not sessions else "wake"
            sessions.append(self)

        async def __aenter__(self):
            events.append(self.name + "_enter")
            return self

        async def __aexit__(self, *args):
            events.append(self.name + "_exit")

        async def commit(self):
            events.append(self.name + "_commit")

    uid = uuid.uuid4()
    sources = [checkpoint.SourceMessage(1, "user", "normal", "Synthetic source"),
               checkpoint.SourceMessage(2, "moly", "normal", "Synthetic reply")]
    plan = checkpoint.plan(sources, previous=None, memory_generation=0,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION, through_message_id=2, reason="input_pressure")
    monkeypatch.setattr(worker, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(worker.config_store, "get_config_values", AsyncMock(return_value={
        "context_retention_v2_enabled": True}))
    monkeypatch.setattr(context_retention, "adopted_user", AsyncMock(return_value=True))
    monkeypatch.setattr(repo, "load_user_state", AsyncMock(return_value=(None, "ko")))
    monkeypatch.setattr(worker, "_generation_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_still_applicable", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(worker, "_load_sources", AsyncMock(return_value=sources))
    monkeypatch.setattr(context_retention, "processing_complete", AsyncMock(return_value=False))
    monkeypatch.setattr(context_retention, "privacy_epoch_matches", AsyncMock(return_value=True), raising=False)
    monkeypatch.setattr(repo, "count", AsyncMock(return_value=0))
    monkeypatch.setattr(checkpoint, "summarize_progress", AsyncMock(return_value=("",
        {**context_retention.empty_state(), "sequence": 1}, SimpleNamespace(billable=0))))

    async def reserve(session, **kwargs):
        assert session is sessions[0]
        events.append("reserve_contract")

    async def wake_dependency(session, **kwargs):
        assert session is sessions[1] and "read_commit" in events
        assert kwargs == {"user_id": uid, "generation": 0, "through_id": 2}
        events.append("wake_dependency")
        return True

    monkeypatch.setattr(context_retention, "enqueue_contract_processing", reserve)
    monkeypatch.setattr(repo, "wake_checkpoint_ingest", wake_dependency)
    result = await worker.handle_checkpoint(SimpleNamespace(user_id=uid, payload=plan.payload(),
        id=uuid.uuid4(), attempt=1))
    assert result.result_code == "checkpoint_draft_ready"
    checkpoint.summarize_progress.assert_awaited_once()
    assert events == ["read_enter", "reserve_contract", "read_commit", "wake_enter",
                      "wake_dependency", "wake_commit", "wake_exit", "read_exit"]


async def test_dependency_wake_failure_preserves_deferral_and_never_logs_exception_body(monkeypatch, caplog):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(worker, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(repo, "wake_checkpoint_ingest", AsyncMock(side_effect=RuntimeError("SYNTHETIC_PRIVATE_TEXT")))
    await worker._wake_ingest_dependency(SimpleNamespace(user_id=uuid.uuid4(), memory_generation=0,
                                                        through_message_id=2))
    assert "checkpoint_dependency_wake_failed" in caplog.text
    assert "SYNTHETIC_PRIVATE_TEXT" not in caplog.text
