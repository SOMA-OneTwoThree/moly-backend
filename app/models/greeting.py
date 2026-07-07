"""greetings — 선발화 발급 보관(미커밋). 대화 이력 아님(ERD §5.1).

일·컨텍스트당 1건 캐시(재호출 = 동일 건, LLM 재호출 없음). 유저 응답 시 messages(kind=greeting)로 커밋.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Greeting(Base):
    __tablename__ = "greetings"
    __table_args__ = (UniqueConstraint("user_id", "context", "activity_date"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    context: Mapped[str] = mapped_column(String)  # onboarding|home_enter|morning|evening|comeback
    content: Mapped[str] = mapped_column(String)
    activity_date: Mapped[date] = mapped_column(Date)
    committed_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
