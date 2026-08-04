"""캐피 일기의 정본.

``kind``가 제품 의미(welcome/shared_day/capi_day)를 소유하고 ``source``는 생성 방식
(llm/preset/welcome/none)을 남기는 감사 필드다. ``diary_date``는 v1 호환 alias이며 새 코드의
날짜 정본은 ``display_date``/``activity_date``다.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class Diary(Base):
    __tablename__ = "diaries"
    __table_args__ = (UniqueConstraint("user_id", "id", name="diaries_user_id_id_uq"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    # v1 compatibility alias. New uniqueness never relies on this column.
    diary_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    activity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    display_date: Mapped[date] = mapped_column(Date)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    author: Mapped[str] = mapped_column(String, server_default=text("'capi'"))
    occurred_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    occurred_timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    occurred_timezone_provenance: Mapped[str | None] = mapped_column(String, nullable=True)
    primary_subject: Mapped[str | None] = mapped_column(String, nullable=True)
    about_tags: Mapped[list[str]] = mapped_column(ARRAY(String), server_default=text("'{}'::text[]"))
    content_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    record_status: Mapped[str] = mapped_column(String, server_default=text("'published'"))
    deleted_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    source: Mapped[str] = mapped_column(String)  # llm | preset | welcome | none
    preset_ment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    content: Mapped[str] = mapped_column(String)
    weather: Mapped[str] = mapped_column(String)  # sunny | cloudy | rainy | windy
    published_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    first_read_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        _TZ, server_default=text("now()"), nullable=True
    )
