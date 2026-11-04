"""chat_contexts — 유저별 대화 앵커와 정규화 기억 처리 좌표."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, select, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, column_property, mapped_column

from app.core.db import Base
from app.models.conversation_checkpoint import ConversationCheckpoint


class ChatContext(Base):
    __tablename__ = "chat_contexts"
    __table_args__ = (CheckConstraint(
        "retention_pressure_through_message_id IS NULL OR retention_pressure_through_message_id > 0",
        name="chat_contexts_retention_pressure_positive_check",
    ),)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    anchor_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # forget마다 +1 → 늦게 돌아온 잡의 stale 결과를 버리는 기준.
    memory_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # Immutable metadata only: the old reader's visible boundary and opaque
    # legacy document identities. Existing messages/documents are never copied.
    retention_compat_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Opaque legacy prose has no item-level evidence. After an explicit
    # correction/withdrawal it cannot safely be injected again as background.
    retention_compat_summary_invalidated_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Reader adoption survives a disabled/deleted rollout setting. This is a
    # correlated read-only expression, not another database column.
    retention_v5_adopted: Mapped[bool] = column_property(
        select(ConversationCheckpoint.id).where(
            ConversationCheckpoint.user_id == user_id,
            ConversationCheckpoint.memory_generation == memory_generation,
            ConversationCheckpoint.version == "conversation-checkpoint-summary-v5",
            text("conversation_checkpoints.publish_state = 'published'"),
        ).exists().correlate_except(ConversationCheckpoint)
    )
    # 대화 turn 커밋마다 +1로 배정(memory_source_turns의 watermark).
    memory_source_watermark: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # fact/insight의 **실제** 내용·source·상태가 바뀐 트랜잭션에서만 정확히 +1.
    # no-op/retry/임베딩 재색인은 증가시키지 않는다(프로필 재생성 폭주 방지).
    relationship_profile_input_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # 채팅 커밋 CAS와 replay hydration의 좌표. Phase 1 lease가 잡은 revision/turn_seq와
    # Phase 2 현재값이 다르면 늦은 결과를 publish하지 않는다.
    context_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_committed_turn_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # 기억/프로필/집중 참조가 달라져 안정 프롬프트 prefix를 무효화할 때만 증가한다.
    prompt_cache_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    contract_processed_through_message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    contract_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # 저장 완료된 원문의 포함 종료 경계. 일반 pending_anchor와 구분한다.
    retention_pressure_through_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
