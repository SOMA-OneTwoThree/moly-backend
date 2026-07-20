"""memory_ingestion_states — 장기기억 일별 추출 watermark와 재시도 상태."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Integer, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class MemoryIngestionState(Base):
    __tablename__ = "memory_ingestion_states"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    activity_date: Mapped[date] = mapped_column(Date, primary_key=True)
    through_message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_attempted_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
