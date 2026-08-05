"""AI 원가 원장 — 호출 전 `started` 기록 → 완료 시 usage/비용 fenced update.

기억 재설계 1단계(docs/capi-memory-ARCHITECTURE.md 15장 1번).

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

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import ceil

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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


async def load_price(
    session: AsyncSession, *, provider: str, model: str, at: datetime | None = None
) -> PriceRow | None:
    """해당 시점에 유효한 단가. 없으면 None(호출측이 unknown 비용으로 남긴다)."""
    row = (
        await session.execute(
            _PRICE_SQL,
            {"provider": provider, "model": model, "at": at or datetime.now(timezone.utc)},
        )
    ).first()
    if row is None:
        return None
    return PriceRow(
        catalog_version=row[0],
        provider=row[1],
        model=row[2],
        input_micro_usd=row[3],
        cached_input_micro_usd=row[4],
        cache_write_micro_usd=row[5],
        output_micro_usd=row[6],
        embedding_micro_usd=row[7],
    )


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
