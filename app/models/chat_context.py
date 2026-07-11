"""chat_contexts — 유저별 대화 컨텍스트 상태(앵커 + 기억 스냅샷).

앵커: 프롬프트에 넣을 세그먼트 시작 메시지 id(append-only, 리셋 시에만 전진).
memory_text: mem0 렌더 스냅샷(핫패스에서 mem0 제거). memory_refreshed_at로 갱신 판정.
민감 데이터(기억 평문 사본) → RLS deny-default + profiles ON DELETE CASCADE(마이그레이션).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ChatContext(Base):
    __tablename__ = "chat_contexts"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    anchor_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    memory_text: Mapped[str | None] = mapped_column(String, nullable=True)
    memory_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
