"""chat_contexts — 유저별 대화 앵커와 정규화 기억 처리 좌표."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ChatContext(Base):
    __tablename__ = "chat_contexts"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    anchor_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # forget마다 +1 → 늦게 돌아온 잡의 stale 결과를 버리는 기준.
    memory_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
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
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
