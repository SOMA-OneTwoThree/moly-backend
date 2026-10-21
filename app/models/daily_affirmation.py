"""daily_affirmations — 날짜별 오늘의 글귀. 운영자가 대시보드에서 날짜마다 한 줄씩 넣는다."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class DailyAffirmation(Base):
    __tablename__ = "daily_affirmations"

    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    text_ko: Mapped[str] = mapped_column(Text)
    text_en: Mapped[str] = mapped_column(Text)
    text_ja: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
