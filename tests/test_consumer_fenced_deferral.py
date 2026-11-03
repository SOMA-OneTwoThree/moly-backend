"""Finalize 도중의 일시 경합은 같은 lease/job을 보존한다. 외부 연결 없음."""
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import jobs
from worker import consumer


class Result:
    def __init__(self, row=None):
        self.row = row
    def first(self):
        return self.row


@pytest.fixture
def fenced(monkeypatch):
    identity, token = uuid.uuid4(), uuid.uuid4()
    deadline = datetime.now(timezone.utc)+timedelta(minutes=1)
    payload = {"mode": "publish_retention", "completed_state": {"synthetic": "draft"}, "privacy_epoch": 0}
    original = jobs.ClaimedJob(identity, "content", "conversation_checkpoint", None, "synthetic",
        payload, 1, 3, "synthetic-worker", token, deadline, None)
    row = {"state": "running", "attempt": 1, "max_attempts": 3, "payload": copy.deepcopy(payload),
           "lease_owner": original.lease_owner, "lease_token": token, "lease_until": deadline,
           "domain": [], "result_code": None}
    control = SimpleNamespace(events=[], steal_after_rollback=False)
    class Session:
        async def __aenter__(self):
            self.before = copy.deepcopy(row)
            return self
        async def __aexit__(self, error_type, error, tb):
            if error_type and not getattr(self, "rolled_back", False):
                await self.rollback()
        async def execute(self, sql, params):
            matches = (row["state"] == "running" and row["lease_owner"] == params["worker_id"]
                       and row["lease_token"] == params["lease_token"])
            if not matches:
                return Result()
            if sql is jobs._SUCCESS_SQL:
                row["state"] = "succeeded"
                control.events.append("success_fence")
                return Result((identity, None))
            assert sql is jobs._DEFER_SQL
            control.events.append("defer_same_lease")
            row.update(state="ready", max_attempts=row["max_attempts"]+1,
                       result_code=params["reason"], available_at=params["available_at"],
                       lease_owner=None, lease_token=None, lease_until=None)
            return Result((identity,))
        async def rollback(self):
            row.clear()
            row.update(copy.deepcopy(self.before))
            self.rolled_back = True
            control.events.append("rollback")
            if control.steal_after_rollback:
                row["lease_token"] = uuid.uuid4()
                row["lease_owner"] = "other-worker"
        async def commit(self):
            control.events.append("commit")
    monkeypatch.setattr(consumer, "get_sessionmaker", lambda: Session)
    telemetry = AsyncMock()
    monkeypatch.setattr(consumer.job_telemetry, "record_start", AsyncMock())
    monkeypatch.setattr(consumer.job_telemetry, "record_outcome", telemetry)
    return SimpleNamespace(job=original, row=row, control=control, telemetry=telemetry)


async def test_fenced_busy_rolls_back_then_defers_same_job_without_spending_failure_budget(fenced, monkeypatch):
    e = fenced
    async def apply(session):
        e.row["domain"].append("not_committed")
        raise consumer.JobDeferred("checkpoint_privacy_busy", delay_s=5)
    async def handler(job):
        return consumer.JobResult(apply_domain=apply)
    monkeypatch.setitem(consumer._REGISTRY, e.job.job_type, handler)
    before = datetime.now(timezone.utc)
    await consumer.run_job(e.job, jobs.queue_config("content"))
    assert e.control.events == ["success_fence", "rollback", "defer_same_lease", "commit"]
    assert e.row["state"] == "ready" and e.row["domain"] == []
    assert e.row["attempt"] == 1 and e.row["max_attempts"] == 4
    assert e.row["max_attempts"]-e.row["attempt"] == e.job.max_attempts
    assert e.row["payload"] == e.job.payload and e.row["lease_token"] is None
    assert before+timedelta(seconds=5) <= e.row["available_at"] <= datetime.now(timezone.utc)+timedelta(seconds=5)
    assert e.telemetry.await_args.args[2:] == (consumer.job_telemetry.OUTCOME_RETRYABLE, "checkpoint_privacy_busy")


async def test_busy_deferral_never_updates_a_later_workers_lease(fenced, monkeypatch):
    e = fenced
    e.control.steal_after_rollback = True
    async def apply(session):
        raise consumer.JobDeferred("checkpoint_privacy_busy", delay_s=5)
    async def handler(job):
        return consumer.JobResult(apply_domain=apply)
    monkeypatch.setitem(consumer._REGISTRY, e.job.job_type, handler)
    await consumer.run_job(e.job, jobs.queue_config("content"))
    assert e.row["state"] == "running" and e.row["lease_owner"] == "other-worker"
    assert e.row["max_attempts"] == 3 and e.row["payload"] == e.job.payload
    assert "defer_same_lease" not in e.control.events


async def test_other_finalize_errors_keep_existing_reaper_behavior(fenced, monkeypatch):
    e = fenced
    async def apply(session):
        raise RuntimeError("synthetic ordinary DB failure")
    async def handler(job):
        return consumer.JobResult(apply_domain=apply)
    monkeypatch.setitem(consumer._REGISTRY, e.job.job_type, handler)
    await consumer.run_job(e.job, jobs.queue_config("content"))
    assert e.row["state"] == "running" and e.row["max_attempts"] == 3
    assert "defer_same_lease" not in e.control.events
