"""AI 원가 계측 — 단가 catalog · 호출 원장 · 잡 시도 telemetry.

기억 재설계 1단계(docs/ARCHITECTURE-capi.md 11.2절). 구조 전환 **전에** 깔아서
legacy 비용까지 같은 표면으로 재고 전환 전/후를 같은 지표로 비교한다.

⚠️ 사용자 quota와 회사 원가는 다른 값이다. quota(`daily_token_limit`)는 기존 billable weighted
unit이 계속 담당하고, 이 원장은 provider 단가 기반 USD 원가만 적재한다. 섞어 쓰지 않는다.
로직·SQL은 `app/services/usage_ledger.py`.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# lane — 동기 대화인지 백그라운드 잡인지. 사용자 quota 체감과 원가 분석을 가르는 축.
LANE_FOREGROUND = "foreground"
LANE_BACKGROUND = "background"

# status — started로 시작해 completed/failed로 수렴한다. 응답을 잃으면 unknown_usage로 남기고
# 0원으로 숨기지 않는다.
STATUS_STARTED = "started"
STATUS_COMPLETED = "completed"
STATUS_UNKNOWN_USAGE = "unknown_usage"
STATUS_FAILED = "failed"


class AiPriceCatalog(Base):
    """effective-dated 단가표. 가격 변경은 새 catalog_version 행 추가로만 한다.

    단가 단위 = micro-USD / 1M tokens ($1.00/1M = 1_000_000). NULL은 '그 모델에 해당 요금 없음'이고
    0과 구분한다 — GPT-4.1 mini의 cache write처럼 지원 여부가 불명확한 값을 0으로 적으면 공짜로
    계산된다.
    """

    __tablename__ = "ai_price_catalog"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    catalog_version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    input_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached_input_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_write_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    embedding_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_note: Mapped[str | None] = mapped_column(String, nullable=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AiUsageLedger(Base):
    """LLM/embedding 호출 1건. 호출 **전** started를 남기고 완료 시 usage를 채운다."""

    __tablename__ = "ai_usage_ledger"

    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # 계정 삭제 시 ON DELETE SET NULL — 집계는 남기고 사람과의 연결만 끊는다.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    turn_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    activity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    lane: Mapped[str] = mapped_column(String, nullable=False)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    model_snapshot: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'started'"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    embedding_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # provider가 cache-write usage를 직접 주지 않아 추정한 경우 true. "정확한 실비" 주장 금지 표식.
    cache_write_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    provider_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    price_catalog_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # unknown_usage 행의 catalog 단가 기반 상한. 0원으로 숨기지 않기 위한 보수 추정.
    cost_upper_bound_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    schema_version: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    # 강제 유실 fault 실험 행 — 정상 completeness 분모에서 빼되 누락시키지 않는다.
    experiment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class JobAttempt(Base):
    """잡 시도 1회. `async_jobs`는 현재 상태만 갖고, 시도별 이력은 여기 남는다.

    retry 분포·lease 상실·dead 도달 경로를 사후 분석하기 위한 telemetry이며 잡 실행의 정본이 아니다.
    """

    __tablename__ = "job_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("async_jobs.id"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    queue: Mapped[str] = mapped_column(String, nullable=False)
    job_type: Mapped[str] = mapped_column(String, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # succeeded | retryable | dead | cancelled | lease_lost | timeout
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
