"""attendance_streak_seeds — 연속 출석이 이어받은 루틴 연속(ERD: 경제와 구독). 유저당 1행, 첫 출석 체크 때 고정."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AttendanceStreakSeed(Base):
    __tablename__ = "attendance_streak_seeds"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # through_date까지 streak일을 출석한 날로 본다. best_streak는 루틴 최장 연속(최장 연속의 하한).
    through_date: Mapped[date] = mapped_column(Date)
    streak: Mapped[int] = mapped_column(Integer)
    best_streak: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
