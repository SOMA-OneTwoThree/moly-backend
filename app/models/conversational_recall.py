"""대화 직렬화, 회상 suppression/projection, grounding/reference/focus 모델.

범용 polymorphic graph를 만들지 않는다. 공개 reference는 현재 diary card만 영속화하고, 사실·episode
ID는 grounding/focus 내부에서 문자열 좌표로만 보관한 뒤 매 조회에서 tenant/suppression을 재검증한다.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKeyConstraint, Integer, String, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class ChatActiveTurn(Base):
    __tablename__ = "chat_active_turns"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    turn_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    base_context_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class ChatResponseReference(Base):
    __tablename__ = "chat_response_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "reply_message_id"], ["messages.user_id", "messages.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "diary_id"], ["diaries.user_id", "diaries.id"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    reply_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String, server_default=text("'diary-reference-v1'"))
    domain: Mapped[str] = mapped_column(String, server_default=text("'diary'"))
    mode: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, server_default=text("'available'"))
    diary_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    rendered_metadata: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    redacted_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    redaction_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class ConversationFocus(Base):
    __tablename__ = "conversation_focus"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    domain: Mapped[str] = mapped_column(String, nullable=False)
    facet: Mapped[str | None] = mapped_column(String, nullable=True)
    reference_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    context_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    expires_turn_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class RecallSuppressionOperation(Base):
    __tablename__ = "memory_suppression_operations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    cut_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    future_learning: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class RecallSuppression(Base):
    __tablename__ = "memory_recall_suppressions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "message_id"], ["messages.user_id", "messages.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    span_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    span_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_hash: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class EpisodicMessage(Base):
    __tablename__ = "memory_episodic_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "message_id"], ["messages.user_id", "messages.id"],
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    index_version: Mapped[str] = mapped_column(String, nullable=False)
    suppression_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class DiaryClaimSource(Base):
    __tablename__ = "diary_claim_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "diary_id"], ["diaries.user_id", "diaries.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "message_id"], ["messages.user_id", "messages.id"],
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    diary_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class DiaryRecallDocument(Base):
    __tablename__ = "diary_recall_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "diary_id"], ["diaries.user_id", "diaries.id"],
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    diary_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    search_text: Mapped[str] = mapped_column(String, nullable=False)
    source_hash: Mapped[str] = mapped_column(String, nullable=False)
    suppression_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    index_version: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class PrivacySubjectBarrier(Base):
    __tablename__ = "privacy_subject_barriers"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    state: Mapped[str] = mapped_column(String, nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    high_watermark: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class PrivacyLedgerEvent(Base):
    __tablename__ = "privacy_ledger_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event: Mapped[str] = mapped_column(String, nullable=False)
    high_watermark: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))
