"""잡 플랫폼(W7) — `async_jobs` 큐의 enqueue/claim/finalize/heartbeat/reaper.

설계 불변식(어기면 조용히 깨진다):
 1. **attempt는 claim 시점에 증가**한다(claim 트랜잭션 안). finalize는 다시 증가시키지 않는다.
    이래야 크래시로 finalize를 못 한 잡도 재클레임마다 카운트돼 반드시 dead에 도달한다
    (poison job 무한루프 방지).
 2. **claim은 `ready`만** 대상으로 한다. 만료 lease 회수(reaper)를 claim 쿼리/트랜잭션에 섞지 않는다.
 3. **finalize·heartbeat은 fencing UPDATE**: `id + state='running' + lease_owner + lease_token`.
    0행이면 lease를 잃은 것이므로 **도메인 반영도 하지 않는다**(늦게 돌아온 소비자의 오확정 차단).
 4. **reaper는 (1)terminal running → (2)retryable running → (3)terminal ready 를 각각
    별도 트랜잭션·commit**으로 돈다. 한 트랜잭션으로 합치지 않는다.
 5. terminal(succeeded/dead/cancelled)에서 같은 행을 ready로 되살리지 않는다. **dead 자동 삭제 없음.**
    운영 replay는 `dedup_key='replay:{old_job_id}:{operation_id}'`인 **새 행**으로만 만든다.
 6. dead 전이는 commit 이후에 PII 없는 구조화 로그 + Slack best-effort 경보를 낸다.
    **경보 실패가 job 상태를 롤백하지 않는다.**

외부 호출(LLM·푸시 등) 중에는 row lock도 세션도 쥐지 않는다 — 핸들러가 결과를 만들어 오면
finalize가 짧은 트랜잭션에서 fencing UPDATE + 도메인 반영 + 후속 enqueue를 함께 커밋한다.
"""
from __future__ import annotations

import json
import hashlib
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import slack_notify

_log = logging.getLogger("moly-worker")

# 큐 5종 — 별도 스키마가 아니라 `queue` 컬럼 값이며 소비자 내부 슬롯으로만 분리한다.
QUEUE_CRITICAL = "critical"                    # 결제
QUEUE_INTERACTIVE_ASYNC = "interactive_async"  # 대화 후속
QUEUE_CONTENT = "content"                      # 일기·요약·반추
QUEUE_NOTIFICATION = "notification"            # 저녁 푸시
QUEUE_MAINTENANCE = "maintenance"
# 기억 색인 전용. content(일기·요약)와 섞으면 일기 300건이 도는 동안 기억이 통째로 밀린다 —
# content concurrency가 1이라 둘이 서로를 막는다(감사 지적).
QUEUE_MEMORY = "memory"
QUEUES: tuple[str, ...] = (
    QUEUE_CRITICAL,
    QUEUE_INTERACTIVE_ASYNC,
    QUEUE_CONTENT,
    QUEUE_MEMORY,
    QUEUE_NOTIFICATION,
    QUEUE_MAINTENANCE,
)

# reaper/finalize가 기록하는 표준 오류 코드(경보 dedup 키의 일부라 값이 계약이다).
ERROR_LEASE_EXPIRED = "lease_expired"
ERROR_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
ERROR_EXPIRED = "expired"

_MAX_BACKOFF_EXP = 32  # 2**(attempt-1) 폭주 방지(어차피 cap에 걸리지만 거대 정수 연산을 피한다)


@dataclass(frozen=True, slots=True)
class QueueConfig:
    """큐별 소비자 실행값. 값은 env 전용(`Settings`) — app_config hot override 대상이 아니다."""

    name: str
    concurrency: int
    claim_batch: int
    timeout_s: float
    lease_s: float
    max_attempts: int
    # 0이면 heartbeat 없음(짧은 잡). 불변식: heartbeat <= min(lease/3, 20s).
    heartbeat_s: float = 0.0


def queue_config(queue: str) -> QueueConfig:
    """`Settings`의 `job_<queue>_*` 필드를 QueueConfig로. 미등록 큐는 오타 은폐 대신 ValueError."""
    if queue not in QUEUES:
        raise ValueError(f"알 수 없는 큐: {queue!r}")

    def _v(suffix: str) -> Any:
        return getattr(settings, f"job_{queue}_{suffix}")

    return QueueConfig(
        name=queue,
        concurrency=int(_v("concurrency")),
        claim_batch=int(_v("claim_batch")),
        timeout_s=float(_v("timeout_s")),
        lease_s=float(_v("lease_s")),
        max_attempts=int(_v("max_attempts")),
        heartbeat_s=float(_v("heartbeat_s")),
    )


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """claim된 잡 1건 + 그 claim의 lease. finalize는 이 lease_token으로만 확정할 수 있다."""

    id: uuid.UUID
    queue: str
    job_type: str
    user_id: uuid.UUID | None
    dedup_key: str
    payload: dict
    attempt: int  # claim 시점에 이미 증가된 값(1-base)
    max_attempts: int
    lease_owner: str
    lease_token: uuid.UUID
    lease_until: datetime
    expires_at: datetime | None


def _as_dict(v: Any) -> dict:
    """jsonb 컬럼 — 드라이버 코덱에 따라 dict 또는 str로 온다. 양쪽 모두 dict로 정규화."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v:
        try:
            parsed = json.loads(v)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _job_from_row(row: Any) -> ClaimedJob:
    return ClaimedJob(
        id=row["id"],
        queue=row["queue"],
        job_type=row["job_type"],
        user_id=row["user_id"],
        dedup_key=row["dedup_key"],
        payload=_as_dict(row["payload"]),
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_until=row["lease_until"],
        expires_at=row["expires_at"],
    )


# ─────────────────────────────────────────────────────────────
# enqueue — (job_type, dedup_key) UNIQUE로 멱등. 구·신 producer가 겹치는 이관 구간의 합류점.
# ─────────────────────────────────────────────────────────────
_ENQUEUE_SQL = text("""
INSERT INTO async_jobs
  (queue, job_type, user_id, dedup_key, payload, payload_hash, payload_schema_version,
   priority, available_at, expires_at, max_attempts)
VALUES
  (:queue, :job_type, :user_id, :dedup_key, CAST(:payload AS jsonb), :payload_hash,
   :payload_schema_version, :priority,
   COALESCE(:available_at, now()), :expires_at, :max_attempts)
ON CONFLICT (job_type, dedup_key) DO NOTHING
RETURNING id
""")


async def enqueue(
    session: AsyncSession,
    *,
    queue: str,
    job_type: str,
    dedup_key: str,
    payload: dict | None = None,
    user_id: uuid.UUID | str | None = None,
    priority: int = 100,
    available_at: datetime | None = None,
    expires_at: datetime | None = None,
    max_attempts: int | None = None,
) -> uuid.UUID | None:
    """잡 1건 등록. 이미 같은 `(job_type, dedup_key)`가 있으면 **아무것도 하지 않고 None**(멱등).

    **커밋하지 않는다** — 호출측 트랜잭션 경계에 합류시켜 도메인 변경과 원자적으로 등록되게 한다
    (finalize 안에서 후속 잡을 거는 경로가 이 성질에 의존한다).
    """
    cfg = queue_config(queue)
    payload_wire = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    row = (
        await session.execute(
            _ENQUEUE_SQL,
            {
                "queue": queue,
                "job_type": job_type,
                "user_id": user_id,
                "dedup_key": dedup_key,
                "payload": payload_wire,
                "payload_hash": hashlib.sha256(payload_wire.encode("utf-8")).hexdigest(),
                "payload_schema_version": str((payload or {}).get("schema_version", "job-payload-v1")),
                "priority": priority,
                "available_at": available_at,
                "expires_at": expires_at,
                "max_attempts": max_attempts if max_attempts is not None else cfg.max_attempts,
            },
        )
    ).first()
    return row[0] if row is not None else None


_REPLAY_DEAD_SQL = text("""
INSERT INTO async_jobs
  (queue, job_type, user_id, dedup_key, replay_of, replay_operation_id,
   payload, payload_hash, payload_schema_version, priority,
   available_at, expires_at, max_attempts)
SELECT j.queue, j.job_type, j.user_id,
       'replay:' || j.id::text || ':' || CAST(:operation_id AS text),
       j.id, CAST(:operation_id AS uuid), j.payload, j.payload_hash, j.payload_schema_version,
       j.priority, now(), j.expires_at, j.max_attempts
FROM async_jobs j
WHERE j.id=:job_id AND j.state='dead'
  AND (j.expires_at IS NULL OR j.expires_at > now())
  AND j.payload_redacted_at IS NULL
  AND (j.payload_expires_at IS NULL OR j.payload_expires_at > now())
ON CONFLICT (job_type,dedup_key) DO NOTHING
RETURNING id
""")


async def replay_dead(
    session: AsyncSession, *, job_id: uuid.UUID, operation_id: uuid.UUID
) -> uuid.UUID | None:
    """dead 원본을 불변으로 보존하고 같은 payload의 새 잡을 만든다. 커밋은 호출측 소유."""
    row = (
        await session.execute(
            # 같은 bind를 dedup 문자열과 uuid 열에 함께 쓰므로 wire는 text로 고정하고 SQL에서
            # uuid 위치만 명시 cast한다. asyncpg의 한 parameter-one-type 규칙을 따른다.
            _REPLAY_DEAD_SQL, {"job_id": job_id, "operation_id": str(operation_id)}
        )
    ).first()
    return row[0] if row is not None else None


# ─────────────────────────────────────────────────────────────
# claim — ready만. lease reclaim을 섞지 않는다(불변식 2).
# ─────────────────────────────────────────────────────────────
_CLAIM_SQL = text("""
WITH candidate AS (
  SELECT id FROM async_jobs
  WHERE queue=:queue AND state='ready' AND available_at <= now()
    AND attempt < max_attempts AND (expires_at IS NULL OR expires_at > now())
  ORDER BY priority ASC, available_at ASC, created_at ASC
  FOR UPDATE SKIP LOCKED LIMIT :batch_size
)
UPDATE async_jobs j
SET state='running', attempt=j.attempt+1, lease_owner=:worker_id,
    lease_token=gen_random_uuid(), lease_until=now()+make_interval(secs => :lease_seconds)
FROM candidate c WHERE j.id=c.id
RETURNING j.id, j.queue, j.job_type, j.user_id, j.dedup_key, j.payload, j.attempt,
          j.max_attempts, j.lease_owner, j.lease_token, j.lease_until, j.expires_at
""")


async def claim(
    session: AsyncSession,
    queue: str,
    *,
    worker_id: str,
    batch_size: int | None = None,
    lease_s: float | None = None,
) -> list[ClaimedJob]:
    """`ready` 잡을 batch_size만큼 claim(FOR UPDATE SKIP LOCKED → 소비자 간 중복 0).

    `attempt`가 여기서 증가하고 커밋된다 — 이 프로세스가 곧바로 죽어도 그 시도는 이미 셈에 들어간다.
    """
    cfg = queue_config(queue)
    rows = (
        await session.execute(
            _CLAIM_SQL,
            {
                "queue": queue,
                "batch_size": cfg.claim_batch if batch_size is None else batch_size,
                "worker_id": worker_id,
                "lease_seconds": cfg.lease_s if lease_s is None else lease_s,
            },
        )
    ).mappings().all()
    await session.commit()  # claim은 자체 완결 트랜잭션(가시성 확보 후 핸들러 실행)
    return [_job_from_row(r) for r in rows]


# ─────────────────────────────────────────────────────────────
# finalize — 전부 fencing(불변식 3). 0행이면 도메인 반영 없이 rollback.
# ─────────────────────────────────────────────────────────────
_FENCE = "WHERE id=:id AND state='running' AND lease_owner=:worker_id AND lease_token=:lease_token"

_SUCCESS_SQL = text(f"""
UPDATE async_jobs
SET state='succeeded', result_code=:result_code, result_detail=CAST(:result_detail AS jsonb),
    finished_at=now(), payload_expires_at=COALESCE(payload_expires_at,now()+interval '24 hours'),
    lease_owner=NULL, lease_token=NULL, lease_until=NULL
{_FENCE}
  AND (user_id IS NULL OR NOT EXISTS (
    -- 차단 조건은 행 존재가 아니라 state다. active 행은 정상 사용자다.
    SELECT 1 FROM privacy_subject_barriers b
    WHERE b.user_id=async_jobs.user_id AND b.state <> 'active'
  ))
RETURNING id
""")

_RETRYABLE_SQL = text(f"""
UPDATE async_jobs
SET state=CASE WHEN attempt >= max_attempts THEN 'dead' ELSE 'ready' END,
    available_at=CASE WHEN attempt >= max_attempts THEN available_at ELSE :retry_at END,
    finished_at=CASE WHEN attempt >= max_attempts THEN now() ELSE NULL END,
    last_error_code=:error_code, last_error_at=now(),
    lease_owner=NULL, lease_token=NULL, lease_until=NULL
{_FENCE}
RETURNING state, attempt
""")

_TERMINAL_SQL = text(f"""
UPDATE async_jobs
SET state=:state, result_code=:result_code, result_detail=CAST(:result_detail AS jsonb),
    finished_at=now(), last_error_code=:error_code, last_error_at=now(),
    lease_owner=NULL, lease_token=NULL, lease_until=NULL
{_FENCE}
RETURNING id
""")

_HEARTBEAT_SQL = text(f"""
UPDATE async_jobs SET lease_until = now() + make_interval(secs => :lease_seconds)
{_FENCE}
RETURNING lease_until
""")

_SUBJECT_BLOCKED_SQL = text(
    "SELECT EXISTS(SELECT 1 FROM privacy_subject_barriers "
    "WHERE user_id=:user_id AND state <> 'active')"
)

_SCRUB_RETENTION_SQL = text("""
WITH scrub_jobs AS (
  UPDATE async_jobs SET payload='{}'::jsonb,result_detail=NULL,payload_redacted_at=now()
  WHERE state IN ('succeeded','dead','cancelled') AND payload_redacted_at IS NULL
    AND payload_expires_at IS NOT NULL AND payload_expires_at<=now()
  RETURNING 1
), scrub_idempotency AS (
  UPDATE idempotency_keys SET response=NULL,redacted_at=now()
  WHERE response IS NOT NULL AND response_expires_at IS NOT NULL AND response_expires_at<=now()
  RETURNING 1
)
SELECT (SELECT count(*) FROM scrub_jobs),(SELECT count(*) FROM scrub_idempotency)
""")


async def subject_blocked(session: AsyncSession, user_id: uuid.UUID | None) -> bool:
    if user_id is None:
        return False
    return bool(await session.scalar(_SUBJECT_BLOCKED_SQL, {"user_id": user_id}))


async def scrub_expired_payloads(session: AsyncSession) -> tuple[int, int]:
    row = (await session.execute(_SCRUB_RETENTION_SQL)).first()
    await session.commit()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def _fence_params(job: ClaimedJob) -> dict:
    return {"id": job.id, "worker_id": job.lease_owner, "lease_token": job.lease_token}


def _lost_lease(job: ClaimedJob, what: str) -> None:
    """lease를 잃은 늦은 소비자의 확정 시도 — 관측만(정상 동작이고, 도메인은 반영되지 않았다)."""
    _log.warning(
        "잡 %s 무시(lease 상실) — job_id=%s queue=%s job_type=%s attempt=%s",
        what, job.id, job.queue, job.job_type, job.attempt,
    )


async def finalize_success(
    session: AsyncSession,
    job: ClaimedJob,
    *,
    result_code: str = "ok",
    result_detail: dict | None = None,
    apply_domain: Callable[[AsyncSession], Awaitable[None]] | None = None,
) -> bool:
    """성공 확정 + 도메인 반영 + 후속 enqueue를 **한 짧은 트랜잭션**으로.

    fencing UPDATE가 0행이면(lease 상실) `apply_domain`을 **호출하지 않고** rollback한다.
    True=확정됨 / False=lease 상실(다른 소비자가 이미 이어받았다).
    """
    row = (
        await session.execute(
            _SUCCESS_SQL,
            {
                **_fence_params(job),
                "result_code": result_code,
                "result_detail": json.dumps(result_detail) if result_detail is not None else None,
            },
        )
    ).first()
    if row is None:
        await session.rollback()
        _lost_lease(job, "성공 확정")
        return False
    if apply_domain is not None:
        await apply_domain(session)
    await session.commit()
    return True


async def finalize_retryable(
    session: AsyncSession,
    job: ClaimedJob,
    *,
    error_code: str,
    retry_after_s: float | None = None,
    now: datetime | None = None,
) -> str | None:
    """재시도 가능 실패. attempt(=claim 때 증가분 포함)가 소진됐으면 즉시 dead로 끝낸다.

    반환 `'ready'`(재시도 예약) | `'dead'` | `None`(lease 상실 — 아무것도 바꾸지 않음).
    """
    retry_at = next_retry_at(job.attempt, now=now, retry_after_s=retry_after_s)
    row = (
        await session.execute(
            _RETRYABLE_SQL,
            {**_fence_params(job), "error_code": error_code, "retry_at": retry_at},
        )
    ).first()
    if row is None:
        await session.rollback()
        _lost_lease(job, "재시도 확정")
        return None
    state = row[0]
    await session.commit()
    if state == "dead":  # 경보는 commit 이후 — 전송 실패가 상태를 되돌리지 않게(불변식 6)
        await notify_dead(job, error_code=error_code)
    return state


async def finalize_terminal(
    session: AsyncSession,
    job: ClaimedJob,
    *,
    state: str,
    error_code: str,
    result_detail: dict | None = None,
) -> bool:
    """재시도 없이 즉시 종료. `state='dead'`(스키마 검증 실패·미지원 payload 등) 또는
    `'cancelled'`(expires_at 경과·대상 유저 삭제)."""
    if state not in ("dead", "cancelled"):
        raise ValueError(f"terminal 확정에 쓸 수 없는 상태: {state!r}")
    row = (
        await session.execute(
            _TERMINAL_SQL,
            {
                **_fence_params(job),
                "state": state,
                "result_code": error_code,
                "result_detail": json.dumps(result_detail) if result_detail is not None else None,
                "error_code": error_code,
            },
        )
    ).first()
    if row is None:
        await session.rollback()
        _lost_lease(job, f"{state} 확정")
        return False
    await session.commit()
    if state == "dead":
        await notify_dead(job, error_code=error_code)
    return True


async def heartbeat(
    session: AsyncSession, job: ClaimedJob, *, lease_s: float | None = None
) -> bool:
    """lease 연장 — fencing 조건 동일. False면 이미 lease를 잃었다(핸들러를 중단해야 한다)."""
    cfg = queue_config(job.queue)
    row = (
        await session.execute(
            _HEARTBEAT_SQL,
            {**_fence_params(job), "lease_seconds": cfg.lease_s if lease_s is None else lease_s},
        )
    ).first()
    await session.commit()
    return row is not None


# ─────────────────────────────────────────────────────────────
# backoff — equal-jitter 지수. attempt가 claim 때 이미 증가했다는 전제.
# ⚠️ base 2.0s / cap 60.0s 는 잠정 운영 시작값이다 — 실제 429 Retry-After·복구시간 **측정 후 조정 필요**.
# ─────────────────────────────────────────────────────────────
def next_retry_at(
    attempt: int, *, now: datetime | None = None, retry_after_s: float | None = None
) -> datetime:
    """`raw = min(cap, base * 2**(attempt-1))`, `delay = uniform(raw/2, raw)`.

    provider가 유효한 `Retry-After`를 주면 그보다 이르게 재시도하지 않는다(둘 중 늦은 쪽).
    """
    now = now or datetime.now(timezone.utc)
    exp = min(max(attempt, 1) - 1, _MAX_BACKOFF_EXP)
    raw_s = min(settings.job_backoff_cap_s, settings.job_backoff_base_s * (2**exp))
    at = now + timedelta(seconds=random.uniform(raw_s / 2, raw_s))
    if retry_after_s is not None and retry_after_s > 0:
        at = max(at, now + timedelta(seconds=retry_after_s))
    return at


# ─────────────────────────────────────────────────────────────
# reaper — 큐별로 (1)terminal running → (2)retryable running → (3)terminal ready.
# **각 단계는 별도 트랜잭션·commit**(불변식 4). 합치면 한 단계의 실패가 나머지를 되돌린다.
# ⚠️ interval 10s / batch 50 은 잠정값 — queue oldest age와 statement DB p95 보고 **측정 후 조정 필요**.
# ─────────────────────────────────────────────────────────────
_REAP_TERMINAL_RUNNING_SQL = text("""
WITH candidate AS (
  SELECT id FROM async_jobs
  WHERE queue=:queue AND state='running' AND lease_until < now()
    AND ((expires_at IS NOT NULL AND expires_at <= now()) OR attempt >= max_attempts)
  ORDER BY CASE WHEN expires_at IS NOT NULL AND expires_at <= now() THEN 0 ELSE 1 END,
           lease_until, id
  FOR UPDATE SKIP LOCKED LIMIT :reap_batch_size
)
UPDATE async_jobs j SET
  state=CASE WHEN j.expires_at IS NOT NULL AND j.expires_at <= now() THEN 'cancelled' ELSE 'dead' END,
  finished_at=now(),
  last_error_code=CASE WHEN j.expires_at IS NOT NULL AND j.expires_at <= now()
                       THEN 'expired' ELSE 'attempts_exhausted' END,
  last_error_at=now(), lease_owner=NULL, lease_token=NULL, lease_until=NULL
FROM candidate c WHERE j.id=c.id
RETURNING j.id, j.queue, j.job_type, j.attempt, j.state, j.last_error_code
""")

_REAP_RETRYABLE_RUNNING_SQL = text("""
WITH candidate AS (
  SELECT id FROM async_jobs
  WHERE queue=:queue AND state='running' AND lease_until < now()
    AND (expires_at IS NULL OR expires_at > now()) AND attempt < max_attempts
  ORDER BY lease_until, id
  FOR UPDATE SKIP LOCKED LIMIT :reap_batch_size
)
UPDATE async_jobs j SET state='ready', available_at=now(), finished_at=NULL,
  last_error_code='lease_expired', last_error_at=now(),
  lease_owner=NULL, lease_token=NULL, lease_until=NULL
FROM candidate c WHERE j.id=c.id
RETURNING j.id
""")

_REAP_TERMINAL_READY_SQL = text("""
WITH candidate AS (
  SELECT id FROM async_jobs
  WHERE queue=:queue AND state='ready'
    AND ((expires_at IS NOT NULL AND expires_at <= now()) OR attempt >= max_attempts)
  ORDER BY available_at, id
  FOR UPDATE SKIP LOCKED LIMIT :reap_batch_size
)
UPDATE async_jobs j SET
  state=CASE WHEN j.expires_at IS NOT NULL AND j.expires_at <= now() THEN 'cancelled' ELSE 'dead' END,
  finished_at=now(),
  last_error_code=CASE WHEN j.expires_at IS NOT NULL AND j.expires_at <= now()
                       THEN 'expired' ELSE 'attempts_exhausted' END,
  last_error_at=now()
FROM candidate c WHERE j.id=c.id
RETURNING j.id, j.queue, j.job_type, j.attempt, j.state, j.last_error_code
""")


async def _reap_phase(
    session: AsyncSession, sql: Any, queue: str, batch_size: int
) -> list[dict]:
    """한 단계 = 한 트랜잭션. UPDATE 후 즉시 commit하고 그 결과 행을 돌려준다."""
    rows = (
        await session.execute(sql, {"queue": queue, "reap_batch_size": batch_size})
    ).mappings().all()
    out = [dict(r) for r in rows]
    await session.commit()
    return out


async def _alert_dead_rows(rows: list[dict]) -> None:
    """reaper가 dead로 확정한 행 경보 — **commit 이후**. cancelled는 정상 만료라 경보 대상이 아니다."""
    for r in rows:
        if r.get("state") == "dead":
            await notify_dead_fields(
                job_id=r["id"], queue=r["queue"], job_type=r["job_type"],
                attempt=r["attempt"], error_code=r.get("last_error_code") or "unknown",
            )


async def reap_queue(
    session: AsyncSession, queue: str, *, batch_size: int | None = None
) -> dict[str, int]:
    """한 큐 1주기. 반환 = 단계별 처리 행수(관측용)."""
    n = settings.job_reaper_batch_size if batch_size is None else batch_size
    terminal_running = await _reap_phase(session, _REAP_TERMINAL_RUNNING_SQL, queue, n)
    await _alert_dead_rows(terminal_running)
    requeued = await _reap_phase(session, _REAP_RETRYABLE_RUNNING_SQL, queue, n)
    terminal_ready = await _reap_phase(session, _REAP_TERMINAL_READY_SQL, queue, n)
    await _alert_dead_rows(terminal_ready)
    return {
        "terminal_running": len(terminal_running),
        "requeued": len(requeued),
        "terminal_ready": len(terminal_ready),
    }


# ─────────────────────────────────────────────────────────────
# dead 경보 — PII 없는 구조화 로그 + Slack best-effort. 실패해도 절대 예외를 올리지 않는다.
# ─────────────────────────────────────────────────────────────
async def notify_dead_fields(
    *, job_id: Any, queue: str, job_type: str, attempt: int, error_code: str
) -> None:
    """payload·user_id는 싣지 않는다(PII). 놓친 경보는 /health/queues의 dead count가 보완한다."""
    _log.error(
        "잡 dead — job_id=%s queue=%s job_type=%s attempt=%s error_code=%s",
        job_id, queue, job_type, attempt, error_code,
    )
    try:
        await slack_notify.alert(
            f"☠️ [잡 dead] queue={queue} job_type={job_type} "
            f"attempt={attempt} error={error_code} job_id={job_id}",
            # 같은 (queue, job_type, error_code)는 alert_dedup_window_sec 동안 억제(스톰 방지)
            dedup_key=f"job_dead:{queue}:{job_type}:{error_code}",
        )
    except Exception as e:  # noqa: BLE001  # 경보 실패가 job 상태를 되돌리면 안 된다(불변식 6)
        _log.warning("잡 dead 경보 전송 실패(무시): %r", e)


async def notify_dead(job: ClaimedJob, *, error_code: str) -> None:
    await notify_dead_fields(
        job_id=job.id, queue=job.queue, job_type=job.job_type,
        attempt=job.attempt, error_code=error_code,
    )


# ─────────────────────────────────────────────────────────────
# 큐 상태 — /health/queues(이관 게이트). dead count 증가·미확인 1건 이상이면 배포 gate 실패로 본다.
# ─────────────────────────────────────────────────────────────
_STATS_SQL = text("""
SELECT queue, state, count(*) AS n,
       EXTRACT(EPOCH FROM (now() - min(COALESCE(finished_at, created_at))))::double precision
         AS oldest_age_s
FROM async_jobs
WHERE state IN ('ready','running','dead')
GROUP BY queue, state
""")


async def queue_stats(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """큐별 ready/running/dead count + oldest dead age(초). 잡이 없는 큐도 0으로 항상 포함한다."""
    out: dict[str, dict[str, Any]] = {
        q: {"ready": 0, "running": 0, "dead": 0, "oldest_dead_age_s": None} for q in QUEUES
    }
    for r in (await session.execute(_STATS_SQL)).mappings().all():
        bucket = out.setdefault(  # 미등록 queue 값(구버전 잔재)도 은폐하지 않고 노출
            r["queue"], {"ready": 0, "running": 0, "dead": 0, "oldest_dead_age_s": None}
        )
        bucket[r["state"]] = int(r["n"])
        if r["state"] == "dead" and r["oldest_age_s"] is not None:
            bucket["oldest_dead_age_s"] = int(r["oldest_age_s"])
    return out
