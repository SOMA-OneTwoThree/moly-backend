"""잡 시도 telemetry — 기록은 남되, 실패해도 잡을 막지 않는다."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.services import job_telemetry as jt
from app.services.jobs import ClaimedJob


def _job(attempt: int = 1) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        queue="content",
        job_type="memory_extract",
        user_id=uuid.uuid4(),
        dedup_key="d1",
        payload={},
        attempt=attempt,
        max_attempts=3,
        lease_owner="W1",
        lease_token=uuid.uuid4(),
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=60),
        expires_at=None,
    )


class _Session:
    def __init__(self, sink: list, *, raises: Exception | None = None):
        self.sink = sink
        self.raises = raises

    async def execute(self, stmt, params=None):
        if self.raises is not None:
            raise self.raises
        self.sink.append((str(stmt), params))

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _factory(sink: list, *, raises: Exception | None = None):
    def _maker():
        return lambda: _Session(sink, raises=raises)

    return _maker


async def test_start_records_attempt_coordinates():
    sink: list = []
    job = _job(attempt=2)
    await jt.record_start(job, _factory(sink))
    sql, params = sink[0]
    assert "INSERT INTO job_attempts" in sql
    assert params["job_id"] == job.id and params["attempt"] == 2
    assert params["worker_id"] == "W1" and params["queue"] == "content"


async def test_start_is_idempotent_per_attempt():
    """같은 시도를 두 번 기록해도 행이 늘지 않는다(UNIQUE + DO NOTHING)."""
    sink: list = []
    await jt.record_start(_job(), _factory(sink))
    assert "ON CONFLICT (job_id, attempt) DO NOTHING" in sink[0][0]


async def test_outcome_closes_only_open_attempt():
    """늦게 온 기록이 이미 확정된 결과를 덮지 않는다."""
    sink: list = []
    await jt.record_outcome(_job(), _factory(sink), jt.OUTCOME_DEAD, "unknown_job_type")
    sql, params = sink[0]
    assert "outcome IS NULL" in sql
    assert params["outcome"] == "dead" and params["error_code"] == "unknown_job_type"


async def test_recording_failure_never_propagates():
    """telemetry는 잡 실행의 정본이 아니다 — DB가 죽어도 잡 처리를 막지 않는다."""
    job = _job()
    await jt.record_start(job, _factory([], raises=RuntimeError("db down")))
    await jt.record_outcome(job, _factory([], raises=RuntimeError("db down")), jt.OUTCOME_SUCCEEDED)
