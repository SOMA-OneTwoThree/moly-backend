"""Synthetic proof fixtures only; no DB, provider or Slack calls."""

from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import uuid

import pytest

from app.services import checkpoint, jobs, monitoring_state
from app.services import summary_recovery as recovery
from worker import monitoring

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def proof_fixture():
    owner = uuid.uuid4()
    sources = {
        mid: {
            "id": mid,
            "user_id": owner,
            "sender": "user" if mid % 2 else "moly",
            "kind": "normal",
            "content": f"Synthetic source {mid}",
            "created_at": NOW,
        }
        for mid in range(1, 7)
    }

    def payload(ids):
        rows = [
            checkpoint.SourceMessage(
                mid, sources[mid]["sender"], "normal", sources[mid]["content"], NOW
            )
            for mid in ids
        ]
        return {
            "schema_version": checkpoint.SCHEMA_VERSION,
            "summarizer_version": checkpoint.SUMMARIZER_VERSION,
            "source_message_ids": ids,
            "source_hash": checkpoint.source_hash(previous=None, messages=rows),
            "through_message_id": max(ids),
            "previous_checkpoint_id": None,
            "previous_through_message_id": None,
            "memory_generation": 0,
        }

    failed = {
        "id": uuid.uuid4(),
        "user_id": owner,
        "queue": "content",
        "job_type": "conversation_checkpoint",
        "state": "dead",
        "attempt": 3,
        "max_attempts": 3,
        "last_error_code": "summary_invalid",
        "payload": payload([1, 2, 3, 4]),
        "payload_redacted_at": None,
        "finished_at": NOW - timedelta(minutes=20),
    }
    succeeded = {
        "id": uuid.uuid4(),
        "user_id": owner,
        "job_type": "conversation_checkpoint",
        "state": "succeeded",
        "payload": payload([1, 2, 3, 4, 5, 6]),
        "payload_redacted_at": None,
    }
    for job in (failed, succeeded):
        job["payload_hash"] = recovery._wire_hash(job["payload"])
    publication = {
        "id": uuid.uuid4(),
        "user_id": owner,
        "memory_generation": 0,
        "preparation_id": None,
        "publish_state": "published",
        "summary": "Synthetic complete summary.",
        "created_at": NOW,
        "source_hash": succeeded["payload"]["source_hash"],
        "version": checkpoint.SUMMARIZER_VERSION,
        "through_message_id": 6,
    }
    return failed, succeeded, publication, sources


def verified(failed, success, publication, sources, **kwargs):
    return recovery.verified_replacement(
        failed,
        success,
        publication,
        generation=kwargs.get("generation", 0),
        parents=kwargs.get("parents", {}),
        source_rows=sources,
    )


def test_exact_source_superset_and_both_hashes_establish_recovery():
    f, s, c, m = proof_fixture()
    assert verified(f, s, c, m)
    assert set(f["payload"]["source_message_ids"]) < set(s["payload"]["source_message_ids"])


@pytest.mark.parametrize(
    "fault",
    [
        "boundary_only",
        "different_owner",
        "different_generation",
        "different_version",
        "different_namespace",
        "unpublished",
        "changed_source",
        "changed_kind",
        "changed_time",
        "missing_source",
        "failure_hash",
        "success_hash",
        "published_hash",
        "payload_hash",
        "redacted_failure",
        "redacted_success",
        "not_exhausted",
        "critical_error",
        "unsuccessful",
        "old_publication",
        "duplicate_source",
        "wrong_parent",
    ],
)
def test_no_false_recovery_or_critical_alert_suppression(fault):
    f, s, c, m = proof_fixture()
    if fault == "boundary_only":
        s["payload"]["source_message_ids"] = [5, 6]
        s["payload"]["source_hash"] = checkpoint.source_hash(
            previous=None,
            messages=[
                checkpoint.SourceMessage(mid, m[mid]["sender"], "normal", m[mid]["content"], NOW)
                for mid in [5, 6]
            ],
        )
        s["payload_hash"] = recovery._wire_hash(s["payload"])
        c["source_hash"] = s["payload"]["source_hash"]
    elif fault == "different_owner":
        s["user_id"] = uuid.uuid4()
    elif fault == "different_generation":
        assert not verified(f, s, c, m, generation=1)
        return
    elif fault == "different_version":
        c["version"] = checkpoint.LEGACY_SUMMARIZER_VERSION
    elif fault == "different_namespace":
        c["preparation_id"] = uuid.uuid4()
    elif fault == "unpublished":
        c["publish_state"] = "draft"
    elif fault == "changed_source":
        m[1]["content"] += " changed"
    elif fault == "changed_kind":
        m[1]["kind"] = "safety"
    elif fault == "changed_time":
        m[1]["created_at"] += timedelta(seconds=1)
    elif fault == "missing_source":
        del m[1]
    elif fault in {"failure_hash", "success_hash"}:
        job = f if fault == "failure_hash" else s
        job["payload"]["source_hash"] = "0" * 64
        job["payload_hash"] = recovery._wire_hash(job["payload"])
    elif fault == "published_hash":
        c["source_hash"] = "0" * 64
    elif fault == "payload_hash":
        f["payload_hash"] = "0" * 64
    elif fault == "redacted_failure":
        f["payload_redacted_at"] = NOW
    elif fault == "redacted_success":
        s["payload_redacted_at"] = NOW
    elif fault == "not_exhausted":
        f["attempt"] = 2
    elif fault == "critical_error":
        f["last_error_code"] = "summary_name_leak"
    elif fault == "unsuccessful":
        s["state"] = "ready"
    elif fault == "old_publication":
        c["created_at"] = NOW - timedelta(hours=1)
    elif fault == "duplicate_source":
        f["payload"]["source_message_ids"] = [1, 2, 2, 4]
    else:
        f["payload"]["previous_checkpoint_id"] = str(uuid.uuid4())
        f["payload_hash"] = recovery._wire_hash(f["payload"])
    assert not verified(f, s, c, m)


def test_receipt_survives_payload_redaction_and_is_distinct_for_new_incidents():
    f, s, c, m = proof_fixture()
    assert verified(f, s, c, m)
    receipt = recovery.receipt_for(f, s, c, NOW)
    text = json.dumps(receipt)
    assert (
        str(f["user_id"]) not in text
        and "Synthetic" not in text
        and "source_message_ids" not in text
    )
    f["payload"] = {}
    f["payload_redacted_at"] = NOW
    assert recovery.receipt_matches(f, receipt)
    new = deepcopy(f)
    new["payload_hash"] = "a" * 64
    assert not recovery.receipt_matches(new, receipt)
    assert not recovery.receipt_matches(
        f, {"schema": "summary-recovery-v1", "failed_payload_hash": f["payload_hash"]}
    )


def test_fifteen_minute_grace_and_unknown_finished_time():
    f, _, _, _ = proof_fixture()
    for elapsed, count in [(0, 0), (899, 0), (900, 1)]:
        f["finished_at"] = NOW - timedelta(seconds=elapsed)
        snapshot = recovery.Snapshot((f,), frozenset(), {}, True, NOW)
        assert len(snapshot.pending) == count
        assert snapshot.waiting == 1 - count
    f["finished_at"] = None
    assert len(recovery.Snapshot((f,), frozenset(), {}, True, NOW).pending) == 1


@pytest.mark.parametrize(
    "job_type,error,grouped",
    [
        ("conversation_checkpoint", "summary_invalid", True),
        ("conversation_checkpoint", "summary_llm_failed", True),
        ("conversation_checkpoint", "summary_name_leak", False),
        ("conversation_checkpoint", "invalid_payload", False),
        ("privacy_cleanup", "summary_invalid", False),
        ("refund", "summary_invalid", False),
    ],
)
def test_exact_narrow_alert_scope(job_type, error, grouped):
    assert recovery.grouped_failure(job_type, error) is grouped


async def test_only_narrow_summary_errors_skip_immediate_skull(monkeypatch):
    from app.services import slack_notify

    alert = AsyncMock()
    monkeypatch.setattr(slack_notify, "alert", alert)
    for error in sorted(recovery.ERRORS):
        await jobs.notify_dead_fields(
            job_id=uuid.uuid4(),
            queue="content",
            job_type="conversation_checkpoint",
            attempt=3,
            error_code=error,
        )
    alert.assert_not_awaited()
    for kind, error in [
        ("privacy_cleanup", "summary_invalid"),
        ("refund", "failed"),
        ("conversation_checkpoint", "summary_name_leak"),
    ]:
        await jobs.notify_dead_fields(
            job_id=uuid.uuid4(), queue="critical", job_type=kind, attempt=3, error_code=error
        )
    assert alert.await_count == 3


def test_summary_recovery_notice_requires_actual_notification_of_same_incident():
    obs = monitoring_state.Observation(
        "worker_summary", False, "empty", "unused", recovery_requires_notification=True
    )
    state = {"active": True, "had_incident": True, "incident_number": 2}
    assert not monitoring_state.transition(state, obs, NOW)[1]
    assert not monitoring_state.transition(
        {**state, "notified_incident_number": 1, "notified_active": True}, obs, NOW
    )[1]
    assert monitoring_state.transition(
        {**state, "notified_incident_number": 2, "notified_active": True}, obs, NOW
    )[1]
    # Existing signals retain their previous unsent-incident recovery behavior.
    original = monitoring_state.Observation("worker_memory", False, "empty", "unused")
    assert monitoring_state.transition(state, original, NOW)[1]


@pytest.mark.parametrize("state", ["pending", "waiting", "resolved", "unknown", "query_failed"])
async def test_tick_summary_observation_uses_persistent_aggregate_only(monkeypatch, state):
    f, _, _, _ = proof_fixture()
    if state == "waiting":
        f["finished_at"] = NOW
    snapshot = recovery.Snapshot(
        (f,),
        frozenset({str(f["id"])}) if state == "resolved" else frozenset(),
        {},
        state != "unknown",
        NOW,
    )
    monkeypatch.setattr(
        recovery,
        "snapshot",
        AsyncMock(
            return_value=snapshot, side_effect=RuntimeError() if state == "query_failed" else None
        ),
    )
    persist = AsyncMock()
    monkeypatch.setattr(recovery, "persist_receipts", persist)

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    monkeypatch.setattr(monitoring, "get_sessionmaker", lambda: session)
    publish = AsyncMock()
    monkeypatch.setattr(monitoring_state, "publish", publish)
    await monitoring._summary_observation(NOW)
    observed = {call.args[0].signal: call.args[0] for call in publish.call_args_list}
    assert observed["summary_observation"].active is (state in {"unknown", "query_failed"})
    if state in {"unknown", "query_failed"}:
        assert "worker_summary" not in observed
    else:
        assert (
            observed["worker_summary"].active
            is {"pending": True, "waiting": None, "resolved": False}[state]
        )
        assert observed["worker_summary"].recovery_requires_notification
        assert str(f["id"]) not in observed["worker_summary"].message


def test_original_stats_keeps_all_successful_replay_ancestors():
    sql = str(jobs._STATS_SQL)
    assert "WITH RECURSIVE resolved" in sql and "JOIN resolved x ON j.id = x.id" in sql
    assert "state='succeeded'" in sql and "j.id IN (SELECT id FROM resolved)" in sql
    assert "summary" not in sql


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


def snapshot_session(f, s, c, messages, receipts=None):
    failed = {**f, "current_generation": 0}
    successful = {**s, "checkpoint_id": c["id"]}
    execute = AsyncMock(
        side_effect=[Rows([failed]), Rows([successful]), Rows(list(messages.values())), Rows([c])]
    )
    return SimpleNamespace(execute=execute, scalar=AsyncMock(return_value=receipts or {}))


@pytest.mark.parametrize("mode", ["new", "redacted_receipt", "capacity", "privacy", "generation"])
async def test_runtime_snapshot_is_fail_closed_and_persists_only_verified_metadata(
    monkeypatch, mode
):
    from app.services import privacy

    f, s, c, messages = proof_fixture()
    receipt = recovery.receipt_for(f, s, c, NOW)
    receipts = {str(f["id"]): receipt} if mode == "redacted_receipt" else {}
    if mode == "capacity":
        receipts = {str(uuid.uuid4()): {} for _ in range(recovery.MAX_RECEIPTS)}
    if mode == "redacted_receipt":
        f["payload"] = {}
        f["payload_redacted_at"] = NOW
    if mode == "generation":
        f["payload"]["memory_generation"] = 1
        f["payload_hash"] = recovery._wire_hash(f["payload"])
    barrier = privacy.BarrierState("deleting" if mode == "privacy" else "active", 0, None)
    load = AsyncMock(return_value=barrier)
    monkeypatch.setattr(privacy, "load_barrier", load)
    session = snapshot_session(f, s, c, messages, receipts)
    observed = await recovery.snapshot(session, now=NOW)
    if mode in {"new", "redacted_receipt"}:
        assert observed.recovered == frozenset({str(f["id"])}) and not observed.pending
        assert observed.complete
        assert bool(observed.new_receipts) is (mode == "new")
    else:
        assert not observed.recovered and not observed.new_receipts and len(observed.pending) == 1
        assert observed.complete is (mode != "capacity")
    if mode in {"redacted_receipt", "privacy"}:
        assert session.execute.await_count == 1  # No source/body query.
    if mode == "redacted_receipt":
        load.assert_not_awaited()  # A historical receipt needs no deleted raw.
    assert all(
        not str(call.args[0]).lstrip().startswith(("UPDATE", "DELETE", "INSERT"))
        for call in session.execute.call_args_list
    )


async def test_receipt_write_capacity_failure_cannot_be_reported_as_recovered(monkeypatch):
    f, _, _, _ = proof_fixture()
    snapshot = recovery.Snapshot((f,), frozenset({str(f["id"])}), {str(f["id"]): {}}, True, NOW)
    monkeypatch.setattr(recovery, "snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        recovery,
        "persist_receipts",
        AsyncMock(side_effect=ValueError("summary_recovery_receipt_capacity")),
    )

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    monkeypatch.setattr(monitoring, "get_sessionmaker", lambda: session)
    publish = AsyncMock()
    monkeypatch.setattr(monitoring_state, "publish", publish)
    await monitoring._summary_observation(NOW)
    assert publish.await_count == 1
    assert (
        publish.await_args.args[0].signal == "summary_observation"
        and publish.await_args.args[0].active is True
    )


async def test_health_uses_receipt_hash_and_no_source_body(monkeypatch):
    f, s, c, _ = proof_fixture()
    receipt = recovery.receipt_for(f, s, c, NOW)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value={str(f["id"]): receipt}),
        execute=AsyncMock(return_value=Rows([f])),
    )
    assert await recovery.resolved_receipt_ids(session) == {str(f["id"])}
    sql = str(session.execute.await_args.args[0])
    assert "FROM async_jobs" in sql and "messages" not in sql and "state='dead'" in sql
    f["payload_hash"] = "a" * 64
    assert not await recovery.resolved_receipt_ids(session)


def test_new_job_id_does_not_inherit_an_old_incident_receipt():
    f, s, c, _ = proof_fixture()
    receipts = {str(f["id"]): recovery.receipt_for(f, s, c, NOW)}
    new = {**f, "id": uuid.uuid4()}
    assert not recovery.receipt_matches(new, receipts.get(str(new["id"])))


async def test_receipt_capacity_is_checked_even_on_first_insert():
    values = {str(uuid.uuid4()): {} for _ in range(recovery.MAX_RECEIPTS + 1)}
    session = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(ValueError, match="summary_recovery_receipt_capacity"):
        await recovery.persist_receipts(session, values)
    session.execute.assert_not_awaited()


async def test_source_proof_scan_overflow_is_unknown_not_normal(monkeypatch):
    from app.services import privacy

    f, s, c, messages = proof_fixture()
    failures = [
        {**f, "id": uuid.uuid4(), "current_generation": 0} for _ in range(recovery.MAX_FAILURES + 1)
    ]
    receipts = {str(job["id"]): recovery.receipt_for(job, s, c, NOW) for job in failures}
    session = SimpleNamespace(
        execute=AsyncMock(return_value=Rows(failures)), scalar=AsyncMock(return_value=receipts)
    )
    load = AsyncMock()
    monkeypatch.setattr(privacy, "load_barrier", load)
    snapshot = await recovery.snapshot(session, now=NOW)
    assert not snapshot.complete and len(snapshot.failures) == recovery.MAX_FAILURES
    load.assert_not_awaited()


async def test_summary_observation_is_after_existing_critical_observations(monkeypatch):
    sequence = []

    async def publish(obs, *args, **kwargs):
        sequence.append(obs.signal)

    async def summary(*args):
        sequence.append("summary")

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(monitoring, "get_sessionmaker", lambda: session)
    monkeypatch.setattr(
        monitoring,
        "rc_snapshot",
        AsyncMock(
            return_value={
                env: {"failed": 0, "pending": 0, "fingerprint": "empty", "oldest": None}
                for env in ("PRODUCTION", "UNKNOWN", "SANDBOX")
            }
        ),
    )
    monkeypatch.setattr(monitoring_state, "publish", publish)
    monkeypatch.setattr(monitoring, "_summary_observation", summary)
    await monitoring._emit(NOW, {})
    assert sequence.index("rc_production") < sequence.index("summary")
    assert sequence.index("rc_unknown") < sequence.index("summary")


async def test_stats_additional_exclusion_keeps_other_queues_and_oldest_age(monkeypatch):
    base = Rows(
        [
            {"queue": "content", "state": "dead", "n": 3, "oldest_age_s": 500},
            {"queue": "critical", "state": "dead", "n": 1, "oldest_age_s": 900},
        ]
    )
    remaining = Rows(
        [
            {"queue": "content", "state": "dead", "n": 1, "oldest_age_s": 100},
            {"queue": "critical", "state": "dead", "n": 1, "oldest_age_s": 900},
        ]
    )
    session = SimpleNamespace(execute=AsyncMock(side_effect=[base, remaining]))
    ids = frozenset({str(uuid.uuid4()), str(uuid.uuid4())})
    monkeypatch.setattr(recovery, "resolved_receipt_ids", AsyncMock(return_value=ids))
    values = await jobs.queue_stats(session)
    assert values["content"]["dead"] == 1 and values["content"]["oldest_dead_age_s"] == 100
    assert values["critical"]["dead"] == 1 and values["critical"]["oldest_dead_age_s"] == 900
    sql = str(session.execute.await_args_list[1].args[0])
    assert "WITH RECURSIVE resolved" in sql and "summary_resolved" in sql
    assert set(session.execute.await_args_list[1].args[1]["summary_resolved"]) == {
        uuid.UUID(value) for value in ids
    }
