"""diaries — 캐피의 일기(ERD §5.3). 열람은 등급무관 무료. content는 스냅샷(풀 수정이 과거 일기 안 바꿈).

source: `llm`(개인·대화기반) / `preset`(캐피 자기일기, 우리가 날짜 지정으로 시드) /
`welcome`(가입 첫날) / `none`(tombstone — 지정본 없는 날의 처리완료 마커, published_at=NULL이라
사용자 미노출, SOMA-389). ※ 매일·절대 비지 않음 정책은 SOMA-389로 폐지.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class Diary(Base):
    __tablename__ = "diaries"
    __table_args__ = (UniqueConstraint("user_id", "diary_date"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    diary_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String)  # llm | preset | welcome | none
    preset_ment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    content: Mapped[str] = mapped_column(String)
    weather: Mapped[str] = mapped_column(String)  # sunny | cloudy | rainy | windy
    published_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    first_read_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        _TZ, server_default=text("now()"), nullable=True
    )
