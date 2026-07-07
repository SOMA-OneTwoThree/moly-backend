"""routines / routine_completions — 루틴(ERD §5.5). 주기 = 주 N회. 삭제 = soft delete."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class Routine(Base):
    __tablename__ = "routines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String)
    frequency_per_week: Mapped[int] = mapped_column(SmallInteger)  # 주 N회
    reminder_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reminder_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)  # soft delete
    created_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)


class RoutineCompletion(Base):
    __tablename__ = "routine_completions"
    __table_args__ = (UniqueConstraint("routine_id", "activity_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    routine_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    activity_date: Mapped[date] = mapped_column(Date)
    completed_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
