"""memory_facts / memory_evidence / memory_insights / memory_insight_sources / memory_forget_markers.

정규화 기억의 사실·근거·통찰·망각 마커. **턴 단위 구조화 사실**이고
판정(ADD/REINFORCE/SUPERSEDE/KEEP_BOTH/IGNORE)은 LLM이 아니라 코드가 한다
(`app/services/memory_reconcile.py`). SQL·불변식은 `app/services/memory_repo.py`.

읽을 때 반드시 같이 봐야 하는 것:
- fact의 `(normalization_version, content_hash)`와 marker의 `(normalization_version, normalized_hash)`는
  **같은 산출물**이다. 계산은 `memory_norm.content_hash` 하나뿐이고 forget은 fact 값을 그대로 복사한다.
- fact의 `forgotten`/`superseded`, insight의 `invalidated`/`superseded`는 **terminal**이다.
  같은 내용을 다시 관찰해도 기존 행을 active로 되살리지 않는다(새 행 또는 IGNORE).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKeyConstraint, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class MemoryFact(Base):
    """구조화 사실 1건. 상태 = active | superseded | forgotten(뒤 둘은 terminal)."""

    __tablename__ = "memory_facts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # profile | preference | relationship | event | emotion — 코드 registry(memory_registry.KINDS)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # 자연어 표면. 저장 직전 naming.to_placeholder + 살균을 강제한다(실명 평문 저장 금지).
    canonical_text: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    # registry의 canonical key. cardinality(single|multi)가 SUPERSEDE/KEEP_BOTH를 가른다.
    predicate: Mapped[str | None] = mapped_column(String, nullable=True)
    object_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    event_time: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    valid_from: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    valid_to: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    # 정규화 결과가 달라지는 변경만 새 version을 발급한다. 기존 행을 제자리 재해시하지 않는다.
    normalization_version: Mapped[str] = mapped_column(String, nullable=False)
    # 이 fact를 만든 후보의 최대 source watermark. forget cut 이전/이후를 판별하는 정본 좌표다.
    learned_at_watermark: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # embedding vector 컬럼은 DB에만 둔다 — pgvector 파이썬 패키지가 의존성에 없어 매핑할 타입이
    # 없고, ORM 경로에서 읽고 쓰지도 않는다(검색은 raw SQL). 차원 고정은 embedder 마이그레이션 소관.
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class MemoryEvidence(Base):
    """fact의 근거 메시지. v1 `source_type`은 `conversation_turn`만이다.

    user_id + sender를 FK에 함께 태워 타 유저/assistant 발화를 DB에서도 거부한다. 코드는 같은
    검증을 insert 전에 수행해 친절한 오류와 원자성도 보장한다(`memory_repo.add_evidence`).
    """

    __tablename__ = "memory_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "fact_id"], ["memory_facts.user_id", "memory_facts.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "source_id", "source_sender"],
            ["messages.user_id", "messages.id", "messages.sender"],
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    fact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_type: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_sender: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'user'"))
    source_excerpt_hash: Mapped[str] = mapped_column(String, nullable=False)
    span_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    span_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extractor_version: Mapped[str] = mapped_column(String, server_default=text("'memory-extract-v1'"))
    prompt_version: Mapped[str] = mapped_column(String, server_default=text("'memory-prompt-v1'"))
    observed_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)


class MemoryInsight(Base):
    """fact에서 파생한 통찰. active → invalidated | superseded 만 허용(terminal 불가역)."""

    __tablename__ = "memory_insights"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    text_: Mapped[str] = mapped_column("text", String, nullable=False)  # 컬럼명 text(예약어 아님)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    valid_from: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    valid_to: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    derivation_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class MemoryInsightSource(Base):
    """insight ← fact 간선. 복합 FK(user_id 동반)라 타 유저 fact를 근거로 달 수 없다."""

    __tablename__ = "memory_insight_sources"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    insight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    fact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)


class MemoryForgetMarker(Base):
    """"잊어달라"의 영속 deny key. hard filter가 모든 LLM 제안보다 **먼저** 본다.

    `scope='fact'`는 그 fact의 `content_hash`/`normalization_version`을 그대로 복사한다.
    `expires_at`은 항상 NULL(테이블 CHECK) — 잊은 사실이 만료로 되살아나지 않는다.
    """

    __tablename__ = "memory_forget_markers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)  # fact | predicate | all
    fact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    normalized_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    normalization_version: Mapped[str | None] = mapped_column(String, nullable=True)
    predicate: Mapped[str | None] = mapped_column(String, nullable=True)
    memory_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cut_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # allow=과거 근거만 잊고 이후 새 발화에서 다시 학습 가능, block=향후 동일 사실도 차단.
    future_learning: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'block'")
    )
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    expires_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
