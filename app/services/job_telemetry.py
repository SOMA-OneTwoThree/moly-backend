"""잡 시도 telemetry — `job_attempts` 적재.

기억 재설계 1단계(docs/capi-memory-ARCHITECTURE.md 15장 1번).

`async_jobs` 행은 **현재 상태**만 갖는다. 몇 번째 시도가 왜 실패했는지, lease를 잃었는지, dead까지
어떤 경로였는지는 남지 않아 retry 분포와 SLO를 사후 분석할 수 없다. 이 모듈이 시도 단위 이력을
따로 남긴다.

불변식:
 · 이건 telemetry다. 잡 실행의 정본이 아니며 여기 기록이 실패해도 잡 처리는 그대로 간다.
 · 그래서 모든 함수는 **예외를 올리지 않고** 자기 세션을 짧게 쓴다.
 · `(job_id, attempt)`가 UNIQUE라 같은 시도를 두 번 기록해도 행이 늘지 않는다.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text

from app.services.jobs import ClaimedJob

_log = logging.getLogger("moly-backend")

# outcome 값 — DB CHECK와 같은 집합.
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_RETRYABLE = "retryable"
OUTCOME_DEAD = "dead"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_LEASE_LOST = "lease_lost"
OUTCOME_TIMEOUT = "timeout"

_START_SQL = text("""
INSERT INTO job_attempts (id, job_id, attempt, queue, job_type, worker_id, lease_token, started_at)
VALUES (:id, :job_id, :attempt, :queue, :job_type, :worker_id, :lease_token, now())
ON CONFLICT (job_id, attempt) DO NOTHING
""")

# 아직 안 닫힌 시도만 닫는다 — 늦게 온 기록이 이미 확정된 결과를 덮지 않게.
_FINISH_SQL = text("""
UPDATE job_attempts
SET finished_at=now(),
    duration_ms=GREATEST(0, (EXTRACT(EPOCH FROM (now() - started_at)) * 1000)::int),
    outcome=:outcome,
    error_code=:error_code
WHERE job_id=:job_id AND attempt=:attempt AND outcome IS NULL
""")


async def record_start(job: ClaimedJob, session_factory) -> None:
    """시도 시작. attempt는 claim 시점에 이미 증가한 1-base 값이다.

    `session_factory`는 호출측(consumer)이 넘긴다 — 테스트가 consumer의 팩토리를 갈아끼우면
    telemetry도 같은 대상으로 따라가서 단위테스트가 실 DB에 붙지 않는다.
    """
    try:
        async with session_factory()() as session:
            await session.execute(
                _START_SQL,
                {
                    "id": uuid.uuid4(),
                    "job_id": job.id,
                    "attempt": job.attempt,
                    "queue": job.queue,
                    "job_type": job.job_type,
                    "worker_id": job.lease_owner,
                    "lease_token": job.lease_token,
                },
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001  telemetry 실패가 잡을 막지 않는다
        _log.warning("잡 시도 시작 기록 실패(무시) — job_id=%s: %r", job.id, e)


async def record_outcome(
    job: ClaimedJob, session_factory, outcome: str, error_code: str | None = None
) -> None:
    """시도 종료. 같은 시도를 두 번 닫지 않는다."""
    try:
        async with session_factory()() as session:
            await session.execute(
                _FINISH_SQL,
                {
                    "job_id": job.id,
                    "attempt": job.attempt,
                    "outcome": outcome,
                    "error_code": error_code,
                },
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001
        _log.warning("잡 시도 결과 기록 실패(무시) — job_id=%s: %r", job.id, e)
