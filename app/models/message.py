"""messages — 단일 연속 스레드(ERD §5.2). kind='normal'만 토큰 집계."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    sender: Mapped[str] = mapped_column(String)  # user | moly
    kind: Mapped[str] = mapped_column(String, server_default=text("'normal'"))  # normal | greeting
    content: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activity_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
