"""AI 원가 원장 — 호출 전 `started` 기록 → 완료 시 usage/비용 fenced update.

기억 재설계 1단계(docs/ARCHITECTURE-capi.md 11.2절).

불변식:
 1. **사용자 quota와 회사 원가는 다른 값이다.** quota는 `chat._billable`의 weighted unit이 계속
    담당하고 이 원장은 provider 단가 기반 USD만 적재한다. 백그라운드 장애가 대화 quota를 갉아먹는
    경로를 만들지 않는다.
 2. **응답을 잃은 호출을 0원으로 숨기지 않는다.** 호출 전 `started`를 남기고, 유실은
    `unknown_usage` + catalog 단가 기반 상한 추정으로 보존한다.
 3. **단가는 코드 상수가 아니다.** effective-dated catalog에서 읽고 원장 행에 `price_catalog_version`을
    남겨 가격이 바뀐 뒤에도 과거 비용을 재현한다.
 4. **없는 요금을 0으로 계산하지 않는다.** 필요한 단가가 NULL인데 토큰이 0이 아니면 비용을 확정하지
    않고 `None`을 돌려준다(= 공짜로 집계되는 사고 방지).
 5. 원장 기록 실패가 대화·잡을 깨뜨리지 않는다. 계측은 best-effort이며 호출측이 이를 보장한다.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from math import ceil

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ai_usage import (  # noqa: F401  (상태 상수의 단일 소스)
    LANE_BACKGROUND,
    LANE_FOREGROUND,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_STARTED,
    STATUS_UNKNOWN_USAGE,
)

_log = logging.getLogger("moly-backend")

# 단가 단위 = micro-USD / 1M tokens.
_PER_TOKENS = 1_000_000


@dataclass(frozen=True)
class PriceRow:
    """catalog 한 행. None인 요금은 '그 모델에 해당 요금 없음'이고 0이 아니다."""

    catalog_version: int
    provider: str
    model: str
    input_micro_usd: int | None
    cached_input_micro_usd: int | None
    cache_write_micro_usd: int | None
    output_micro_usd: int | None
    embedding_micro_usd: int | None


def compute_cost(
    price: PriceRow,
    *,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    embedding_tokens: int = 0,
) -> int | None:
    """micro-USD 비용. 필요한 단가가 없으면 `None`(= 확정 불가, 0원 아님).

    토큰이 0인 항목은 단가가 없어도 무방하다 — 쓰지 않은 요금을 요구하지 않는다.
    """
    pairs = (
        (input_tokens, price.input_micro_usd),
        (cached_input_tokens, price.cached_input_micro_usd),
        (cache_write_tokens, price.cache_write_micro_usd),
        (output_tokens, price.output_micro_usd),
        (embedding_tokens, price.embedding_micro_usd),
    )
    total = 0
    for tokens, rate in pairs:
        if not tokens:
            continue
        if rate is None:
            return None  # 없는 요금을 0으로 접지 않는다(불변식 4)
        total += tokens * rate
    # 나눗셈은 마지막에 한 번 — 항목별로 나누면 절삭 오차가 누적된다. 올림으로 과소집계를 피한다.
    return ceil(total / _PER_TOKENS)


_PRICE_SQL = text("""
SELECT catalog_version, provider, model,
       input_micro_usd, cached_input_micro_usd, cache_write_micro_usd,
       output_micro_usd, embedding_micro_usd
FROM ai_price_catalog
WHERE provider=:provider AND model=:model
  AND effective_from <= :at
  AND (effective_to IS NULL OR effective_to > :at)
ORDER BY effective_from DESC, catalog_version DESC
LIMIT 1
""")


# (provider, model) → (monotonic 시각, PriceRow|None). None(단가 없음)도 캐시한다 —
# 미등록 모델이 호출마다 카탈로그를 다시 두드리지 않게. TTL 안에 새 단가가 등록되면
# 최대 TTL만큼 늦게 보이지만, 원장 행에 price_catalog_version이 남아 사후 재계산 가능하다.
_PRICE_CACHE: dict[tuple[str, str], tuple[float, PriceRow | None]] = {}


def _price_cache_clear() -> None:
    """테스트·단가 긴급 반영용."""
    _PRICE_CACHE.clear()


async def load_price(
    session: AsyncSession, *, provider: str, model: str, at: datetime | None = None
) -> PriceRow | None:
    """해당 시점에 유효한 단가. 없으면 None(호출측이 unknown 비용으로 남긴다).

    이 테이블만 프로세스 캐시(TTL 5분)를 허용하는 이유: effective-dated append-only라
    행이 제자리 수정되지 않고, 단가 변경은 사전 등록(미래 effective_from)이 원칙이라
    5분 지연이 과금 정확성을 해치지 않는다. 반면 app_config·게이팅류는 즉시 반영이
    계약이라 캐시하지 않는다. 과거 시점 조회(at 지정)는 캐시를 타지 않는다 —
    캐시 값은 '지금' 유효한 행이므로 과거 창과 다를 수 있다.
    """
    use_cache = at is None
    if use_cache:
        hit = _PRICE_CACHE.get((provider, model))
        if hit is not None and time.monotonic() - hit[0] < settings.price_catalog_cache_ttl_s:
            return hit[1]
    row = (
        await session.execute(
            _PRICE_SQL,
            {"provider": provider, "model": model, "at": at or datetime.now(timezone.utc)},
        )
    ).first()
    price = None if row is None else PriceRow(
        catalog_version=row[0],
        provider=row[1],
        model=row[2],
        input_micro_usd=row[3],
        cached_input_micro_usd=row[4],
        cache_write_micro_usd=row[5],
        output_micro_usd=row[6],
        embedding_micro_usd=row[7],
    )
    if use_cache:
        _PRICE_CACHE[(provider, model)] = (time.monotonic(), price)
    return price


_BEGIN_SQL = text("""
INSERT INTO ai_usage_ledger
  (call_id, user_id, turn_seq, job_id, activity_date, lane, purpose,
   provider, model, status, started_at, attempt, schema_version, prompt_version, experiment_id)
VALUES
  (:call_id, :user_id, :turn_seq, :job_id, :activity_date, :lane, :purpose,
   :provider, :model, 'started', :started_at, :attempt, :schema_version, :prompt_version,
   :experiment_id)
""")


async def begin(
    session: AsyncSession,
    *,
    lane: str,
    purpose: str,
    provider: str,
    model: str,
    user_id: uuid.UUID | None = None,
    turn_seq: int | None = None,
    job_id: uuid.UUID | None = None,
    activity_date: date | None = None,
    attempt: int = 1,
    schema_version: str | None = None,
    prompt_version: str | None = None,
    experiment_id: str | None = None,
    now: datetime | None = None,
) -> uuid.UUID:
    """호출 **전** `started` 행을 만들고 call_id를 돌려준다. 커밋은 호출측 소유."""
    call_id = uuid.uuid4()
    await session.execute(
        _BEGIN_SQL,
        {
            "call_id": call_id,
            "user_id": user_id,
            "turn_seq": turn_seq,
            "job_id": job_id,
            "activity_date": activity_date,
            "lane": lane,
            "purpose": purpose,
            "provider": provider,
            "model": model,
            "started_at": now or datetime.now(timezone.utc),
            "attempt": attempt,
            "schema_version": schema_version,
            "prompt_version": prompt_version,
            "experiment_id": experiment_id,
        },
    )
    return call_id


# started에서만 전진한다 — 이미 수렴한 행을 늦게 돌아온 호출이 덮어쓰지 못하게 하는 fencing.
_COMPLETE_SQL = text("""
UPDATE ai_usage_ledger
SET status='completed',
    completed_at=:completed_at,
    latency_ms=:latency_ms,
    model_snapshot=COALESCE(:model_snapshot, model_snapshot),
    input_tokens=:input_tokens,
    cached_input_tokens=:cached_input_tokens,
    cache_write_tokens=:cache_write_tokens,
    output_tokens=:output_tokens,
    embedding_tokens=:embedding_tokens,
    cache_write_estimated=:cache_write_estimated,
    provider_request_id=:provider_request_id,
    price_catalog_version=:price_catalog_version,
    cost_micro_usd=:cost_micro_usd,
    updated_at=now()
WHERE call_id=:call_id AND status='started'
RETURNING call_id
""")


async def complete(
    session: AsyncSession,
    call_id: uuid.UUID,
    *,
    price: PriceRow | None,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    embedding_tokens: int = 0,
    cache_write_estimated: bool = False,
    model_snapshot: str | None = None,
    provider_request_id: str | None = None,
    latency_ms: int | None = None,
    now: datetime | None = None,
) -> bool:
    """usage와 비용을 확정한다. 단가가 없거나 비용을 확정할 수 없으면 `unknown_usage`로 남긴다.

    반환 True = completed로 수렴, False = unknown_usage(또는 이미 수렴한 행이라 무변경).
    """
    cost = (
        compute_cost(
            price,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            embedding_tokens=embedding_tokens,
        )
        if price is not None
        else None
    )
    if price is None or cost is None:
        # 단가 미등록이거나 필요한 요금이 NULL — 0원으로 접지 않고 미확정으로 남긴다(불변식 2·4).
        await mark_unknown(
            session,
            call_id,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            embedding_tokens=embedding_tokens,
            provider_request_id=provider_request_id,
            error_code="price_unavailable",
            now=now,
        )
        return False
    row = (
        await session.execute(
            _COMPLETE_SQL,
            {
                "call_id": call_id,
                "completed_at": now or datetime.now(timezone.utc),
                "latency_ms": latency_ms,
                "model_snapshot": model_snapshot,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "cache_write_tokens": cache_write_tokens,
                "output_tokens": output_tokens,
                "embedding_tokens": embedding_tokens,
                "cache_write_estimated": cache_write_estimated,
                "provider_request_id": provider_request_id,
                "price_catalog_version": price.catalog_version,
                "cost_micro_usd": cost,
            },
        )
    ).first()
    return row is not None


_UNKNOWN_SQL = text("""
UPDATE ai_usage_ledger
SET status='unknown_usage',
    completed_at=:completed_at,
    input_tokens=:input_tokens,
    cached_input_tokens=:cached_input_tokens,
    cache_write_tokens=:cache_write_tokens,
    output_tokens=:output_tokens,
    embedding_tokens=:embedding_tokens,
    provider_request_id=COALESCE(:provider_request_id, provider_request_id),
    cost_upper_bound_micro_usd=:upper_bound,
    error_code=COALESCE(:error_code, error_code),
    updated_at=now()
WHERE call_id=:call_id AND status='started'
RETURNING call_id
""")


async def mark_unknown(
    session: AsyncSession,
    call_id: uuid.UUID,
    *,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    embedding_tokens: int = 0,
    upper_bound_micro_usd: int | None = None,
    provider_request_id: str | None = None,
    error_code: str | None = None,
    now: datetime | None = None,
) -> bool:
    """응답·usage를 잃은 호출. 0원으로 숨기지 않고 상한 추정과 함께 보존한다."""
    row = (
        await session.execute(
            _UNKNOWN_SQL,
            {
                "call_id": call_id,
                "completed_at": now or datetime.now(timezone.utc),
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "cache_write_tokens": cache_write_tokens,
                "output_tokens": output_tokens,
                "embedding_tokens": embedding_tokens,
                "provider_request_id": provider_request_id,
                "upper_bound": upper_bound_micro_usd,
                "error_code": error_code,
            },
        )
    ).first()
    return row is not None


_FAILED_SQL = text("""
UPDATE ai_usage_ledger
SET status='failed', completed_at=:completed_at, error_code=:error_code,
    latency_ms=COALESCE(:latency_ms, latency_ms), updated_at=now()
WHERE call_id=:call_id AND status='started'
RETURNING call_id
""")


async def mark_failed(
    session: AsyncSession,
    call_id: uuid.UUID,
    *,
    error_code: str,
    latency_ms: int | None = None,
    now: datetime | None = None,
) -> bool:
    """호출 자체가 실패해 토큰을 쓰지 않은 경우(연결 실패·검증 실패 등)."""
    row = (
        await session.execute(
            _FAILED_SQL,
            {
                "call_id": call_id,
                "completed_at": now or datetime.now(timezone.utc),
                "error_code": error_code,
                "latency_ms": latency_ms,
            },
        )
    ).first()
    return row is not None


# ─────────────────────────────────────────────────────────────
# provider 호출 지점에서 쓰는 self-session 래퍼.
#
# 짧은 전용 session을 쓰는 이유: 원장 기록이 대화의 user lock/transaction 안에 들어가면 외부 호출
# 시간만큼 락을 잡게 된다. 계측이 본 경로의 동시성을 갉아먹지 않도록 분리한다.
# 모든 함수는 **절대 예외를 올리지 않는다** — 계측 실패가 대화·잡을 깨뜨리면 안 된다(불변식 5).
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LedgerContext:
    """호출 1건의 귀속 정보. 이게 없으면 원장에 남기지 않는다(계측 opt-in)."""

    lane: str
    purpose: str
    user_id: uuid.UUID | None = None
    turn_seq: int | None = None
    job_id: uuid.UUID | None = None
    activity_date: date | None = None
    attempt: int = 1
    prompt_version: str | None = None
    experiment_id: str | None = None


def with_purpose(ctx: LedgerContext | None, purpose: str) -> LedgerContext | None:
    """같은 귀속에 목적만 바꾼 사본. 한 잡이 여러 목적의 호출을 하는 경우(일기 생성→복원→번역)."""
    return None if ctx is None else replace(ctx, purpose=purpose)


async def open_call(ctx: LedgerContext, *, provider: str, model: str) -> uuid.UUID | None:
    """호출 **전** started 행. 실패하면 None을 돌려주고 호출측은 그대로 진행한다."""
    from app.core.db import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            call_id = await begin(
                session,
                lane=ctx.lane,
                purpose=ctx.purpose,
                provider=provider,
                model=model,
                user_id=ctx.user_id,
                turn_seq=ctx.turn_seq,
                job_id=ctx.job_id,
                activity_date=ctx.activity_date,
                attempt=ctx.attempt,
                prompt_version=ctx.prompt_version,
                experiment_id=ctx.experiment_id,
            )
            await session.commit()
            return call_id
    except Exception as e:  # noqa: BLE001  계측 실패가 본 경로를 막지 않는다
        _log.warning("원장 시작 기록 실패(무시): %r", e)
        return None


# ─────────────────────────────────────────────────────────────
# close 배치 flush(#23b) — 호출당 세션+커밋(왕복 3+)을 없앤다.
#
# `open_call`(started 선커밋)은 그대로다 — 그게 "돈을 쓰기 시작했다"는 유일한 durable 증거라
# 버퍼에 넣으면 crash 때 호출 자체가 사라진다. close만 버퍼에 모아 주기적으로(≤1분) 한
# 세션에서 확정한다. 프로세스가 flush 전에 죽으면 그 행은 'started'로 남는데, 아래
# reconcile_stale_started가 24h 뒤 catalog 상한 추정과 함께 unknown_usage로 수렴시킨다
# (최장 lease 180s ≪ 24h — in-flight 오탐 불가). graceful shutdown flush는 consumer와
# **API 프로세스(챗 lane) 양쪽**에 있어야 한다 — run_close_flusher가 stop 후 마지막으로 민다.
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class _PendingClose:
    call_id: uuid.UUID
    provider: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    embedding_tokens: int
    cache_write_estimated: bool
    model_snapshot: str | None
    provider_request_id: str | None
    latency_ms: int | None
    closed_at: datetime  # flush 지연이 completed_at을 밀지 않게 close 시각을 잡아 둔다


_CLOSE_BUFFER: list[_PendingClose] = []


async def _write_close(session: AsyncSession, item: _PendingClose) -> None:
    price = await load_price(session, provider=item.provider, model=item.model)
    await complete(
        session,
        item.call_id,
        price=price,
        input_tokens=item.input_tokens,
        cached_input_tokens=item.cached_input_tokens,
        cache_write_tokens=item.cache_write_tokens,
        output_tokens=item.output_tokens,
        embedding_tokens=item.embedding_tokens,
        cache_write_estimated=item.cache_write_estimated,
        model_snapshot=item.model_snapshot,
        provider_request_id=item.provider_request_id,
        latency_ms=item.latency_ms,
        now=item.closed_at,
    )


async def flush_closes() -> int:
    """버퍼를 한 세션에서 확정한다. 실패하면 버퍼 앞에 되돌린다(상한 초과분은 reconciler 몫)."""
    if not _CLOSE_BUFFER:
        return 0
    items = list(_CLOSE_BUFFER)
    _CLOSE_BUFFER.clear()  # copy~clear 사이 await 없음 — 유실 창 없음
    from app.core.db import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            for item in items:
                await _write_close(session, item)
            await session.commit()
        return len(items)
    except Exception as e:  # noqa: BLE001  계측 실패가 본 경로를 막지 않는다
        _log.warning("원장 close flush 실패(%d건 되돌림): %r", len(items), e)
        _CLOSE_BUFFER[:0] = items
        overflow = len(_CLOSE_BUFFER) - settings.usage_close_flush_max_buffer
        if overflow > 0:
            # 오래 실패하면 무한 증식 대신 앞(가장 오래된 것)을 버린다 — 그 행들은 'started'로
            # 남아 reconciler가 unknown_usage(상한 추정)로 수렴시킨다. 조용한 소실이 아니다.
            del _CLOSE_BUFFER[:overflow]
            _log.warning("원장 close 버퍼 상한 초과 — %d건은 reconciler로 넘긴다", overflow)
        return 0


# 24h 넘은 started → unknown_usage. 상한 추정은 같은 (provider, model, purpose)의 completed
# 실측 최대 비용 — catalog 단가만으로는 토큰 수를 모른다. 동종 실측이 없으면 NULL(미확정 보존).
_RECONCILE_STALE_SQL = text("""
UPDATE ai_usage_ledger l
SET status='unknown_usage', completed_at=now(),
    cost_upper_bound_micro_usd=b.upper_bound,
    error_code='stale_started_reconciled', updated_at=now()
FROM (
  SELECT s.call_id,
         (SELECT max(c.cost_micro_usd) FROM ai_usage_ledger c
          WHERE c.provider=s.provider AND c.model=s.model AND c.purpose=s.purpose
            AND c.status='completed') AS upper_bound
  FROM ai_usage_ledger s
  WHERE s.status='started' AND s.started_at < now() - interval '24 hours'
  ORDER BY s.started_at LIMIT :limit
) b
WHERE l.call_id = b.call_id AND l.status='started'
""")


async def reconcile_stale_started(limit: int = 200) -> int:
    """flush 유실 창(≤1분)·crash가 남긴 started 잔존을 수렴시킨다. 멱등."""
    from app.core.db import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            res = await session.execute(_RECONCILE_STALE_SQL, {"limit": limit})
            await session.commit()
            n = int(res.rowcount or 0)
            if n:
                _log.warning("stale started %d건 → unknown_usage(상한 추정) 수렴", n)
            return n
    except Exception as e:  # noqa: BLE001
        _log.warning("stale started reconcile 실패(무시): %r", e)
        return 0


async def run_close_flusher(stop, *, reconcile: bool = False) -> None:
    """주기 flush 루프. consumer·API 프로세스 각각 1개 태스크로 돈다. stop 후 마지막 flush.

    reconcile=True는 consumer 쪽 한 곳이면 충분하다(status='started' fencing이라 중복 무해).
    """
    import contextlib

    last_reconcile = 0.0
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.usage_close_flush_interval_s)
        await flush_closes()
        if reconcile and time.monotonic() - last_reconcile >= 3600.0:
            last_reconcile = time.monotonic()
            await reconcile_stale_started()
    await flush_closes()  # graceful shutdown flush — 챗 lane 포함(#23b)


async def close_call(
    call_id: uuid.UUID | None,
    *,
    provider: str,
    model: str,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    embedding_tokens: int = 0,
    cache_write_estimated: bool = False,
    model_snapshot: str | None = None,
    provider_request_id: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """usage를 확정한다. 단가를 못 찾으면 `complete`가 unknown_usage로 남긴다.

    #23b: 기본은 버퍼에 쌓고 flusher가 ≤1분 안에 확정한다. 스위치를 끄면 예전처럼 즉시
    세션을 열어 확정한다(운영 회귀 스위치).
    """
    if call_id is None:
        return
    item = _PendingClose(
        call_id=call_id, provider=provider, model=model,
        input_tokens=input_tokens, cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens, output_tokens=output_tokens,
        embedding_tokens=embedding_tokens, cache_write_estimated=cache_write_estimated,
        model_snapshot=model_snapshot, provider_request_id=provider_request_id,
        latency_ms=latency_ms, closed_at=datetime.now(timezone.utc),
    )
    if settings.usage_close_flush_enabled:
        _CLOSE_BUFFER.append(item)
        if len(_CLOSE_BUFFER) > settings.usage_close_flush_max_buffer:
            del _CLOSE_BUFFER[0]
            _log.warning("원장 close 버퍼 상한 — 가장 오래된 1건을 reconciler로 넘긴다")
        return
    from app.core.db import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            await _write_close(session, item)
            await session.commit()
    except Exception as e:  # noqa: BLE001
        _log.warning("원장 완료 기록 실패(무시): %r", e)


async def close_failed(
    call_id: uuid.UUID | None, *, error_code: str, latency_ms: int | None = None
) -> None:
    """provider 호출이 예외로 끝난 경우. 토큰을 썼는지 알 수 없으면 호출측이 unknown을 택한다."""
    if call_id is None:
        return
    from app.core.db import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            await mark_failed(session, call_id, error_code=error_code, latency_ms=latency_ms)
            await session.commit()
    except Exception as e:  # noqa: BLE001
        _log.warning("원장 실패 기록 실패(무시): %r", e)
