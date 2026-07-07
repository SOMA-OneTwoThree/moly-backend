"""user_daily_stats — 유저 × 앱 기준일 1행(ERD §4.2). 토큰·일일 보상 게이팅."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Integer,
    SmallInteger,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class UserDailyStats(Base):
    __tablename__ = "user_daily_stats"
    __table_args__ = (UniqueConstraint("user_id", "activity_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    activity_date: Mapped[date] = mapped_column(Date)
    tokens_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    ad_reward_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    attendance_claimed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    routine_reward_claimed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
