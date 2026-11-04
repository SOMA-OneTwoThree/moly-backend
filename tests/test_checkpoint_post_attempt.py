"""After-attempt hints accelerate only verified dependency waits, never failure backoff."""
import asyncio
import copy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import checkpoint, context_retention, jobs
from tests import test_retention_worker as legacy_tests
from worker import checkpoint_jobs as worker
from worker import consumer


def job():
    original = legacy_tests.make_job([checkpoint.SourceMessage(1, "user", "normal", "synthetic")])
    return replace(original, payload={**original.payload, "mode": "publish_retention",
        "memory_generation": 3})


@pytest.fixture
def hint_env(monkeypatch):
    publisher = job()
    state = {"complete": False, "enabled": True, "adopted": True, "epoch": 0,
        "generation": 3, "barrier_busy": False, "commits": 0, "reads": 0, "updates": 0,
        "sql": [], "row": {"id": publisher.id, "payload": copy.deepcopy(publisher.payload),
            "state": "ready", "deferred": True, "initial": False, "last_error": None,
            "expired": False, "redacted": False, "busy": False, "due": False}}

    def eligible(params):
        row = state["row"]
        return (row["state"] == "ready" and (row["deferred"] or row["initial"]) and row["last_error"] is None
            and not row["expired"] and not row["redacted"] and not row["due"]
            and row["payload"]["privacy_epoch"] == params["event_epoch"]
            and row["payload"]["memory_generation"] == state["generation"]
            and (params["event_generation"] is None or params["event_generation"] == state["generation"]))

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def execute(self, sql, params):
            state["sql"].append((str(sql), copy.deepcopy(params)))
            if sql is worker._READY_PUBLISHERS:
                state["reads"] += 1
                rows = [(publisher.id, copy.deepcopy(state["row"]["payload"]))] if eligible(params) else []
                return SimpleNamespace(all=lambda: rows)
            assert sql is worker._WAKE_READY_PUBLISHER
            found = (eligible(params) and not state["row"]["busy"]
                and json.loads(params["payload"]) == state["row"]["payload"])
            if found:
                state["row"]["due"] = True
                state["updates"] += 1
            return SimpleNamespace(first=lambda: (publisher.id,) if found else None)
        async def commit(self):
            state["commits"] += 1

    async def complete(*args, **kwargs):
        if state.get("mutate"):
            state["mutate"]()
        return state["complete"]
    async def enabled(*args, **kwargs):
        assert kwargs["lock"] is True
        return state["enabled"]
    async def adopted(*args, **kwargs):
        assert kwargs["lock"] is True
        return state["adopted"]
    async def epoch(*args, **kwargs):
        assert kwargs["lock"] is True
        return not state["barrier_busy"] and kwargs["privacy_epoch"] == state["epoch"]
    monkeypatch.setattr(worker, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(context_retention, "processing_complete", complete)
    monkeypatch.setattr(context_retention, "enabled_now", enabled)
    monkeypatch.setattr(context_retention, "adopted_user", adopted)
    monkeypatch.setattr(context_retention, "privacy_epoch_matches", epoch)
    return state, Session(), publisher


def dependency(publisher):
    # Repair generation is deliberately different from the context generation.
    return replace(publisher, job_type="mem0_consolidate", payload={"privacy_epoch": 0, "repair_generation": 17})


async def test_publisher_ready_before_dependency_commit_is_woken_after_completion(hint_env):
    state, session, publisher = hint_env
    await worker._checkpoint_post_attempt(publisher)
    assert state["updates"] == 0
    state["complete"] = True
    await worker._checkpoint_post_attempt(dependency(publisher))
    assert state["updates"] == state["commits"] == 1


async def test_dependency_before_running_publisher_defer_is_closed_by_own_hint(hint_env):
    state, session, publisher = hint_env
    state["row"]["state"] = "running"
    state["complete"] = True
    await worker._checkpoint_post_attempt(dependency(publisher))
    assert state["updates"] == 0
    state["row"]["state"] = "ready"
    await worker._checkpoint_post_attempt(publisher)
    assert state["updates"] == state["commits"] == 1


async def test_new_publish_wait_child_needs_no_prior_attempt_before_wake(hint_env):
    state, _, publisher = hint_env
    state['complete'] = True
    state['row'].update(deferred=False, initial=True)
    await worker._checkpoint_post_attempt(dependency(publisher))
    assert state['updates'] == state['commits'] == 1
    assert "j.attempt=0 AND j.result_code IS NULL" in str(worker._READY_PUBLISHERS)


async def test_incomplete_helper_acquires_no_job_row_lock_or_update(hint_env):
    state, session, publisher = hint_env
    assert not await worker.wake_ready_publisher(session, job_id=publisher.id,
        user_id=publisher.user_id, payload=publisher.payload, event_epoch=0)
    assert not state["sql"] and state["updates"] == 0


async def test_simultaneous_hints_are_semantically_idempotent(hint_env):
    state, session, publisher = hint_env
    state["complete"] = True
    await asyncio.gather(worker._checkpoint_post_attempt(publisher),
                         worker._checkpoint_post_attempt(dependency(publisher)))
    assert state["updates"] == state["commits"] == 1


@pytest.mark.parametrize("changed", ["generation", "epoch", "source_payload", "enabled", "adopted",
    "expired", "redacted", "busy", "barrier_busy", "last_error", "not_deferred", "running", "dead"])
async def test_metadata_changes_or_busy_rows_cannot_be_accelerated(hint_env, changed):
    state, session, publisher = hint_env
    state["complete"] = True
    def mutate():
        if changed in {"generation", "epoch"}:
            state[changed] += 1
        elif changed in {"enabled", "adopted"}:
            state[changed] = False
        elif changed == "source_payload":
            state["row"]["payload"]["source_hash"] = "changed"
        elif changed == "last_error":
            state["row"]["last_error"] = "unexpected_error"
        elif changed == "not_deferred":
            state["row"]["deferred"] = False
        elif changed in {"running", "dead"}:
            state["row"]["state"] = changed
        elif changed == "barrier_busy":
            state["barrier_busy"] = True
        else:
            state["row"][changed] = True
    state["mutate"] = mutate
    await worker._checkpoint_post_attempt(publisher)
    assert state["updates"] == state["commits"] == 0


async def test_real_error_backoff_is_not_a_dependency_wait_even_with_old_result_code(hint_env):
    state, session, publisher = hint_env
    state["complete"] = True
    state["row"]["last_error"] = "provider_timeout"
    await worker._checkpoint_post_attempt(publisher)
    assert state["updates"] == 0 and state["reads"] == 1
    assert len(state["sql"]) == 1  # discovery only, never locks the row


@pytest.mark.parametrize("kind", ["mem0_ingest", "mem0_consolidate", "contract_compile", "conversation_checkpoint"])
async def test_only_explicit_context_generation_is_compared_not_repair_generation(hint_env, kind):
    state, session, publisher = hint_env
    state["complete"] = True
    payload = {"privacy_epoch": 0, "repair_generation": 123}
    if kind in {"contract_compile", "conversation_checkpoint"}:
        payload["memory_generation"] = 3
    await worker._checkpoint_post_attempt(replace(publisher, job_type=kind, payload=payload))
    assert state["updates"] == 1


@pytest.mark.parametrize("payload", [{"privacy_epoch": 1}, {"privacy_epoch": 0, "memory_generation": 2},
    {}, {"privacy_epoch": False}, {"privacy_epoch": 0, "memory_generation": None}])
async def test_stale_or_unbound_event_never_mutates_current_publisher(hint_env, payload):
    state, session, publisher = hint_env
    state["complete"] = True
    await worker._checkpoint_post_attempt(replace(publisher, payload=payload))
    assert state["updates"] == 0


async def test_due_update_is_only_available_at_and_rechecks_exact_live_metadata(hint_env):
    state, session, publisher = hint_env
    state["complete"] = True
    before = copy.deepcopy(publisher.payload)
    assert await worker.wake_ready_publisher(session, job_id=publisher.id,
        user_id=publisher.user_id, payload=publisher.payload, event_epoch=0, event_generation=3)
    sql, params = state["sql"][-1]
    assert "SET available_at=LEAST(j.available_at,now())" in sql
    assert "FOR UPDATE OF j SKIP LOCKED" in sql
    for fragment in ["j.state='ready'", "j.last_error_code IS NULL", "j.attempt<j.max_attempts",
        "j.payload=CAST(:payload AS jsonb)", "payload_redacted_at IS NULL", "j.expires_at>now()",
        "j.payload_expires_at>now()", "to_jsonb(c.memory_generation)", "CAST(:event_epoch AS bigint)"]:
        assert fragment in sql
    assert json.loads(params["payload"]) == before == publisher.payload
    assert state["commits"] == 0  # helper leaves transaction ownership with caller


@pytest.fixture
def consumer_env(monkeypatch):
    monkeypatch.setattr(consumer, "_POST_ATTEMPT_HOOKS", {})
    wake = Mock()
    monkeypatch.setattr(consumer, "_wake_queues", wake)
    return wake


async def test_hint_runs_only_after_actual_finalize_commit(consumer_env, monkeypatch):
    wake = consumer_env
    publisher = job()
    calls = []
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            calls.append("close")
        async def commit(self):
            calls.append("commit")
    async def finalize(session, *args, **kwargs):
        calls.append("apply")
        await session.commit()
        return True
    async def hint(event):
        assert "commit" in calls and calls[-1] == "close"
        calls.append("hint")
    monkeypatch.setattr(consumer, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(consumer.privacy, "load_barrier", AsyncMock(return_value=None))
    monkeypatch.setattr(consumer.privacy, "authorize_job", lambda *args, **kwargs: SimpleNamespace(allowed=True))
    monkeypatch.setattr(consumer, "_REGISTRY", {publisher.job_type: AsyncMock()})
    monkeypatch.setattr(consumer, "_run_with_heartbeat", AsyncMock(return_value=consumer.JobResult()))
    monkeypatch.setattr(jobs, "finalize_success", finalize)
    monkeypatch.setattr(consumer.job_telemetry, "record_start", AsyncMock())
    monkeypatch.setattr(consumer.job_telemetry, "record_outcome", AsyncMock())
    consumer.register_post_attempt((publisher.job_type,), hint)
    await consumer._run_job_then_wake(publisher, jobs.queue_config("content"))
    assert calls[-1] == "hint"
    wake.assert_called_once()


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_hint_failure_preserves_attempt_result_and_always_wakes_queue(consumer_env, monkeypatch, failure):
    publisher = job()
    original = copy.deepcopy(publisher.payload)
    run = AsyncMock()
    monkeypatch.setattr(consumer, "run_job", run)
    monkeypatch.setattr(consumer, "_POST_ATTEMPT_TIMEOUT_S", 0.001)
    async def hint(event):
        if failure == "error":
            raise RuntimeError("synthetic")
        await asyncio.Event().wait()
    consumer.register_post_attempt((publisher.job_type,), hint)
    await consumer._run_job_then_wake(publisher, jobs.queue_config("content"))
    run.assert_awaited_once()
    consumer_env.assert_called_once()
    assert publisher.payload == original and publisher.attempt == 1 and publisher.max_attempts == 3


async def test_cancelled_attempt_does_not_start_an_additional_hint(consumer_env, monkeypatch):
    publisher = job()
    monkeypatch.setattr(consumer, "run_job", AsyncMock(side_effect=asyncio.CancelledError))
    hint = AsyncMock()
    consumer.register_post_attempt((publisher.job_type,), hint)
    with pytest.raises(asyncio.CancelledError):
        await consumer._run_job_then_wake(publisher, jobs.queue_config("content"))
    hint.assert_not_awaited()
    consumer_env.assert_called_once()


async def test_hint_parent_cancellation_is_not_swallowed(consumer_env, monkeypatch):
    publisher = job()
    monkeypatch.setattr(consumer, "run_job", AsyncMock())
    consumer.register_post_attempt((publisher.job_type,), AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await consumer._run_job_then_wake(publisher, jobs.queue_config("content"))
    consumer_env.assert_called_once()


async def test_unrelated_types_never_run_registered_hints(consumer_env, monkeypatch):
    publisher = job()
    monkeypatch.setattr(consumer, "run_job", AsyncMock())
    hint = AsyncMock()
    consumer.register_post_attempt(worker._PUBLISH_HINT_TYPES, hint)
    await consumer._run_job_then_wake(replace(publisher, job_type="payment_apply"), jobs.queue_config("critical"))
    hint.assert_not_awaited()
