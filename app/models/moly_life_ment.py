"""moly_life_ments — '캐피의 삶' 멘트 풀(ERD §5.4). 임계 미달·미접속 날 일기 소스.

본문은 diaries.content로 스냅샷 복사(풀 수정이 과거 일기를 바꾸지 않게).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class MolyLifeMent(Base):
    __tablename__ = "moly_life_ments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    content: Mapped[str] = mapped_column(String)
    weather: Mapped[str] = mapped_column(String)  # sunny | cloudy | rainy | windy
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Legacy 날짜 지정본. 날짜 없는 legacy 풀은 선택하지 않는다.
    diary_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    week_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sequence_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
