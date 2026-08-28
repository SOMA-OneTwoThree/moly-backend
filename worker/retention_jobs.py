"""장기 수명주기 retention 잡 5종(로드맵 Phase 5) — 무한 증식 테이블의 종식.

실행 기반: pg_cron이 아니라 **maintenance 큐 잡 + tick enqueue**다. tick이 KST hour>=5에
`{job_type}:{KST날짜}` dedup 키로 하루 1회 건다(워커가 05시대에 죽어 있어도 그날 중 self-heal).
실패 관측은 기존 체계 그대로: dead→Slack, /health/queues, job_attempts.

공통 계약:
 · **배치당 커밋** — 한 배치(ctid IN … LIMIT n FOR UPDATE SKIP LOCKED)씩 지우고 커밋한다.
   어느 지점에서 죽어도 지운 만큼만 지워져 있고, 재실행은 멱등이다.
 · 한 잡 실행은 MAX_BATCHES까지만 — timeout 60s·max_attempts 3 안에 수렴하게 보수적으로.
   잔량이 남으면 `:{seq}` 연쇄 재enqueue(privclean 패턴)로 이어간다.
 · priority=200(후순위) — maintenance 큐 concurrency=1에서 탈퇴 청소(privclean)·sweep이 먼저다.

절대 조건(로드맵 표 — 위반 금지):
 · 5-1: 술어는 `dedupe_expires_at IS NOT NULL AND <= now()` 그대로. (30일 키 해방 = 승인된 계약 변화)
 · 5-2: 축은 **KST date**(activity_date 금지 — 74% NULL). `started` 행은 연령 무관 삭제 금지.
   `unknown_usage`는 삭제 제외(보존+경보) — 원본 삭제는 미확정 비용의 0원 확정(불변식 2 위반).
   배치 = 단일 문장(DELETE…RETURNING→INSERT ON CONFLICT 가산) — 어느 지점에서 죽어도 정확히 1회 집계.
 · 5-3: dead·replay 사슬(replay_of 또는 참조되는 원본) 절대 제외.
 · 5-4: `planned` 절대 비대상. pending registry가 참조하는 후보는 **NOT EXISTS로 반드시 제외** —
   커서를 지나친 pending의 재판정(_UNJUDGED_USERS)이 후보 본문을 읽는다. 지우면
   candidate_text_missing → dead 무한 루프 + 그 기억 영구 pending(§9.6 잠복 결함).
 · 5-5: processed만, 365일. failed/pending 무기한(결제 감사 보존).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import config_store, jobs
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobResult

_log = logging.getLogger("moly-worker")

_KST = ZoneInfo("Asia/Seoul")

JOB_RETENTION_IDEMPOTENCY = "retention_idempotency_gc"
JOB_USAGE_ROLLUP = "usage_ledger_rollup"
JOB_RETENTION_JOBS = "retention_jobs_gc"
JOB_MEM0_CANDIDATE_GC = "mem0_candidate_gc"
JOB_RETENTION_RC_EVENTS = "retention_rc_events"

RETENTION_PRIORITY = 200
BATCH = 2000
MAX_BATCHES = 10

DAILY_JOB_TYPES = (
    JOB_RETENTION_IDEMPOTENCY, JOB_USAGE_ROLLUP, JOB_RETENTION_JOBS, JOB_MEM0_CANDIDATE_GC,
)

# ── 5-1: idempotency 키 GC ─────────────────────────────────────
# 계약 변화(승인 항목): dedupe 만료(30일) 지난 키가 지워지면 같은 키 재사용이 409가 아니라
# 신규 처리(과금)가 된다 — openapi conversations.yaml에 30일 경계 명문화 동반.
_IDEMPOTENCY_GC = text("""
DELETE FROM idempotency_keys
WHERE ctid IN (
  SELECT ctid FROM idempotency_keys
  WHERE dedupe_expires_at IS NOT NULL AND dedupe_expires_at <= now()
  LIMIT :n FOR UPDATE SKIP LOCKED
)
""")

# ── 5-2: usage 원장 롤업(90일 이전분) ──────────────────────────
# 축은 (started_at AT TIME ZONE 'Asia/Seoul')::date — activity_date는 74% NULL이라 금지.
# ('Asia/Seoul' 리터럴 캐스트라 tick의 "이상 tz 문자열" 문제와 무관.)
# status 실값은 completed/failed다(로드맵 표기 'succeeded'는 async_jobs 용어 혼입 — 08-29 정정).
# 단일 문장: DELETE…RETURNING을 그대로 집계해 가산 upsert — 어느 지점에서 죽어도
# 트랜잭션 원자성으로 각 행은 정확히 1회 집계된다.
_USAGE_ROLLUP = text("""
WITH del AS (
  DELETE FROM ai_usage_ledger
  WHERE ctid IN (
    SELECT ctid FROM ai_usage_ledger
    -- 술어는 "KST 기준 90일 전 자정"을 timestamptz로 환산한 상수 비교다 —
    -- (started_at AT TIME ZONE 'Asia/Seoul')::date < (KST오늘-90) 과 정확히 동치이면서
    -- (started_at) 인덱스를 탈 수 있다(행마다 캐스트하면 인덱스 불가).
    WHERE status IN ('completed','failed')
      AND started_at <
          (((now() AT TIME ZONE 'Asia/Seoul')::date - 90)::timestamp AT TIME ZONE 'Asia/Seoul')
    LIMIT :n FOR UPDATE SKIP LOCKED
  )
  RETURNING started_at, provider, model, lane, purpose, status,
            input_tokens, cached_input_tokens, cache_write_tokens,
            output_tokens, embedding_tokens, cost_micro_usd
)
INSERT INTO ai_usage_daily_rollup AS r
  (kst_date, provider, model, lane, purpose, status,
   calls, input_tokens, cached_input_tokens, cache_write_tokens,
   output_tokens, embedding_tokens, cost_micro_usd)
SELECT (started_at AT TIME ZONE 'Asia/Seoul')::date, provider, model, lane, purpose, status,
       count(*),
       sum(COALESCE(input_tokens,0)), sum(COALESCE(cached_input_tokens,0)),
       sum(COALESCE(cache_write_tokens,0)), sum(COALESCE(output_tokens,0)),
       sum(COALESCE(embedding_tokens,0)), sum(COALESCE(cost_micro_usd,0))
FROM del
GROUP BY 1,2,3,4,5,6
ON CONFLICT (kst_date, provider, model, lane, purpose, status) DO UPDATE
SET calls = r.calls + EXCLUDED.calls,
    input_tokens = r.input_tokens + EXCLUDED.input_tokens,
    cached_input_tokens = r.cached_input_tokens + EXCLUDED.cached_input_tokens,
    cache_write_tokens = r.cache_write_tokens + EXCLUDED.cache_write_tokens,
    output_tokens = r.output_tokens + EXCLUDED.output_tokens,
    embedding_tokens = r.embedding_tokens + EXCLUDED.embedding_tokens,
    cost_micro_usd = r.cost_micro_usd + EXCLUDED.cost_micro_usd,
    updated_at = now()
""")

# 7일 넘은 started 잔존은 경보(#23b reconciler가 24h에 수렴시키므로 평시 0이어야 한다).
_STARTED_STALE_COUNT = text("""
SELECT count(*) FROM ai_usage_ledger
WHERE status='started' AND started_at < now() - interval '7 days'
""")

# ── 5-3: 완료 잡 GC(14일) + 단명 뮤텍스 고아 청소 ───────────────
_JOBS_GC = text("""
DELETE FROM async_jobs
WHERE ctid IN (
  SELECT j.ctid FROM async_jobs j
  WHERE j.state IN ('succeeded','cancelled')
    AND j.finished_at < now() - interval '14 days'
    AND j.replay_of IS NULL
    AND NOT EXISTS (SELECT 1 FROM async_jobs r WHERE r.replay_of = j.id)
  LIMIT :n FOR UPDATE SKIP LOCKED
)
""")
# 만료 기준 술어 명시(4차 검증) — 살아 있는 클레임/lease(분 단위)와 7일 차이로 오탐 불가.
_ORPHAN_DIARY_CLAIMS = text(
    "DELETE FROM diary_gen_claims WHERE claimed_at < now() - interval '7 days'"
)
_ORPHAN_ACTIVE_TURNS = text(
    "DELETE FROM chat_active_turns WHERE lease_until < now() - interval '7 days'"
)

# ── 5-4: mem0 후보 GC ──────────────────────────────────────────
# consolidated_through_turn_seq는 memory_pipeline_states 소속(§9.6 — candidates에 없다).
_CANDIDATE_GC_COMMITTED = text("""
DELETE FROM mem0_ingest_candidates
WHERE ctid IN (
  SELECT c.ctid FROM mem0_ingest_candidates c
  JOIN memory_pipeline_states s ON s.user_id = c.user_id
  WHERE c.status='committed'
    AND c.turn_seq <= s.consolidated_through_turn_seq
    AND c.updated_at < now() - interval '14 days'
    AND NOT EXISTS (
      SELECT 1 FROM mem0_memory_registry r
      WHERE r.user_id = c.user_id AND r.provider_memory_id = c.provider_memory_id
        AND r.semantic_status = 'pending'
    )
  LIMIT :n FOR UPDATE OF c SKIP LOCKED
)
""")
_CANDIDATE_GC_DEAD = text("""
DELETE FROM mem0_ingest_candidates
WHERE ctid IN (
  SELECT c.ctid FROM mem0_ingest_candidates c
  WHERE c.status='dead' AND c.updated_at < now() - interval '90 days'
    AND NOT EXISTS (
      SELECT 1 FROM mem0_memory_registry r
      WHERE r.user_id = c.user_id AND r.provider_memory_id = c.provider_memory_id
        AND r.semantic_status = 'pending'
    )
  LIMIT :n FOR UPDATE OF c SKIP LOCKED
)
""")

# ── 5-5: RC 이벤트 GC(월 1회) ──────────────────────────────────
_RC_EVENTS_GC = text("""
DELETE FROM revenuecat_events
WHERE ctid IN (
  SELECT ctid FROM revenuecat_events
  WHERE status='processed' AND processed_at IS NOT NULL
    AND processed_at < now() - interval '365 days'
  LIMIT :n FOR UPDATE SKIP LOCKED
)
""")


async def _run_batches(statements: list, *, batch: int = BATCH, max_batches: int = MAX_BATCHES):
    """문장 목록을 순서대로, 각 문장을 배치 반복 실행. 반환 (문장별 삭제 수, 잔량 가능성).

    배치당 커밋 — 중간 실패 시 지운 만큼만 반영돼 있고 재실행이 이어받는다(멱등).
    """
    deleted = [0] * len(statements)
    budget = max_batches
    more = False
    for i, stmt in enumerate(statements):
        while budget > 0:
            async with get_sessionmaker()() as session:
                res = await session.execute(stmt, {"n": BATCH} if ":n" in str(stmt) else {})
                await session.commit()
            n = int(res.rowcount or 0)
            deleted[i] += n
            budget -= 1
            if ":n" not in str(stmt) or n < batch:
                break  # 이 문장은 수렴
        else:
            more = True  # 예산 소진 — 잔량 있음
            break
        if ":n" in str(stmt) and deleted[i] and deleted[i] % batch == 0 and budget == 0:
            more = True
    return deleted, more


async def _record_success(job_type: str) -> None:
    """성공 시각을 app_config에 기록 — /health/deep stale 판정의 단일 소스.

    async_jobs의 finished_at으로 판정할 수 없다: 5-3이 succeeded 잡을 14일에 지우므로
    월간 잡(rc 이벤트 GC)의 성공 증거가 매달 중순에 소멸해 상시 오탐이 된다.
    기록 실패 시 예외 전파 → 잡 재시도(삭제 SQL은 전부 멱등이라 안전).
    """
    async with get_sessionmaker()() as session:
        await config_store.set_config_value(
            session,
            config_store.RETENTION_LAST_SUCCESS_PREFIX + job_type,
            datetime.now(timezone.utc).isoformat(),
        )


def _kst_date(now: datetime | None = None) -> str:
    return (now or datetime.now(_KST)).astimezone(_KST).date().isoformat()


def _continuation(job: ClaimedJob, job_type: str):
    """잔량이 남았을 때 같은 날짜 사슬의 다음 잡을 fenced 트랜잭션에서 건다."""
    seq = int((job.payload or {}).get("seq", 0)) + 1
    date_key = (job.payload or {}).get("date_key") or _kst_date()

    async def _apply(session) -> None:
        await jobs.enqueue(
            session,
            queue=jobs.QUEUE_MAINTENANCE,
            job_type=job_type,
            dedup_key=f"{job_type}:{date_key}:{seq}",
            payload={"seq": seq, "date_key": date_key},
            priority=RETENTION_PRIORITY,
        )

    return _apply


async def handle_retention_idempotency(job: ClaimedJob) -> JobResult:
    deleted, more = await _run_batches([_IDEMPOTENCY_GC])
    await _record_success(JOB_RETENTION_IDEMPOTENCY)
    return JobResult(
        result_code="ok",
        result_detail={"deleted": deleted[0], "more": more},
        apply_domain=_continuation(job, JOB_RETENTION_IDEMPOTENCY) if more else None,
    )


async def handle_usage_rollup(job: ClaimedJob) -> JobResult:
    """롤업 배치의 rowcount는 INSERT 수(집계 후 행 수)라 삭제 수와 다르다 — 수렴 판정은
    '이번 배치의 INSERT 행 수 0 = 지울 것 없음'으로 한다."""
    budget = MAX_BATCHES
    rolled = 0
    more = False
    while budget > 0:
        async with get_sessionmaker()() as session:
            res = await session.execute(_USAGE_ROLLUP, {"n": BATCH})
            await session.commit()
        n = int(res.rowcount or 0)
        rolled += n
        budget -= 1
        if n == 0:
            break
    else:
        more = True
    async with get_sessionmaker()() as session:
        stale = int(await session.scalar(_STARTED_STALE_COUNT) or 0)
    if stale:
        # #23b reconciler가 24h에 수렴시키므로 평시 0 — 남아 있으면 reconciler 고장 신호.
        _log.warning("usage rollup: 7일 넘은 started 잔존 %d건 — reconciler 점검 필요", stale)
    await _record_success(JOB_USAGE_ROLLUP)
    return JobResult(
        result_code="ok",
        result_detail={"rollup_rows": rolled, "stale_started": stale, "more": more},
        apply_domain=_continuation(job, JOB_USAGE_ROLLUP) if more else None,
    )


async def handle_retention_jobs(job: ClaimedJob) -> JobResult:
    deleted, more = await _run_batches([_JOBS_GC, _ORPHAN_DIARY_CLAIMS, _ORPHAN_ACTIVE_TURNS])
    await _record_success(JOB_RETENTION_JOBS)
    return JobResult(
        result_code="ok",
        result_detail={
            "jobs_deleted": deleted[0], "diary_claims": deleted[1],
            "active_turns": deleted[2], "more": more,
        },
        apply_domain=_continuation(job, JOB_RETENTION_JOBS) if more else None,
    )


async def handle_candidate_gc(job: ClaimedJob) -> JobResult:
    deleted, more = await _run_batches([_CANDIDATE_GC_COMMITTED, _CANDIDATE_GC_DEAD])
    await _record_success(JOB_MEM0_CANDIDATE_GC)
    return JobResult(
        result_code="ok",
        result_detail={"committed_deleted": deleted[0], "dead_deleted": deleted[1], "more": more},
        apply_domain=_continuation(job, JOB_MEM0_CANDIDATE_GC) if more else None,
    )


async def handle_rc_events_gc(job: ClaimedJob) -> JobResult:
    deleted, more = await _run_batches([_RC_EVENTS_GC])
    await _record_success(JOB_RETENTION_RC_EVENTS)
    return JobResult(
        result_code="ok",
        result_detail={"deleted": deleted[0], "more": more},
        apply_domain=_continuation(job, JOB_RETENTION_RC_EVENTS) if more else None,
    )


consumer.register(JOB_RETENTION_IDEMPOTENCY, handle_retention_idempotency)
consumer.register(JOB_USAGE_ROLLUP, handle_usage_rollup)
consumer.register(JOB_RETENTION_JOBS, handle_retention_jobs)
consumer.register(JOB_MEM0_CANDIDATE_GC, handle_candidate_gc)
consumer.register(JOB_RETENTION_RC_EVENTS, handle_rc_events_gc)


async def enqueue_daily(session, now: datetime) -> int:
    """tick이 부른다 — KST hour>=5면 오늘분 retention 잡을 건다(dedup으로 하루 1회 수렴).

    `hour==5`가 아니라 `>=5`인 이유: 05시대에 워커가 죽어 있어도 그날 중 아무 틱에서나
    self-heal 된다(§9.6). RC 이벤트 GC는 매월 1일에만 건다.
    """
    kst = now.astimezone(_KST)
    if kst.hour < 5:
        return 0
    date_key = kst.date().isoformat()
    items = [
        (None, f"{jt}:{date_key}", {"seq": 0, "date_key": date_key})
        for jt in DAILY_JOB_TYPES
    ]
    made = 0
    for (user_id, dedup, payload), jt in zip(items, DAILY_JOB_TYPES):
        made += (
            await jobs.enqueue(
                session, queue=jobs.QUEUE_MAINTENANCE, job_type=jt,
                dedup_key=dedup, payload=payload, priority=RETENTION_PRIORITY,
            )
        ) is not None
    if kst.day == 1:
        made += (
            await jobs.enqueue(
                session, queue=jobs.QUEUE_MAINTENANCE, job_type=JOB_RETENTION_RC_EVENTS,
                dedup_key=f"{JOB_RETENTION_RC_EVENTS}:{date_key[:7]}",
                payload={"seq": 0, "date_key": date_key}, priority=RETENTION_PRIORITY,
            )
        ) is not None
    return made
