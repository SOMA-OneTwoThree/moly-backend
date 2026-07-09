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
from sqlalchemy.dialects.postgresql import ARRAY, UUID
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
    frequency_per_week: Mapped[int] = mapped_column(SmallInteger)  # 주 N회(목표 횟수). 요일별이면 len(days_of_week)
    # 요일별 루틴이면 지정 요일(ISO 1=월…7=일). null이면 주 N회(횟수) 모드.
    days_of_week: Mapped[list[int] | None] = mapped_column(ARRAY(SmallInteger), nullable=True)
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
