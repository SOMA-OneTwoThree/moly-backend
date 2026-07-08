"""moly_life_ments — '바라의 삶' 멘트 풀(ERD §5.4). 임계 미달·미접속 날 일기 소스.

본문은 diaries.content로 스냅샷 복사(풀 수정이 과거 일기를 바꾸지 않게).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, text
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
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
