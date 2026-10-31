"""오늘의 작은 실천 — 공용 행동 템플릿과 사용자별 하루 제안."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class DailyPracticeTemplate(Base):
    __tablename__ = "daily_practice_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    title_ko: Mapped[str] = mapped_column(Text, unique=True)
    title_en: Mapped[str] = mapped_column(Text)
    title_ja: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class DailyPracticeOffer(Base):
    __tablename__ = "daily_practice_offers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE")
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("daily_practice_templates.id")
    )
    local_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, server_default=text("'offered'"))
    options_expire_at: Mapped[datetime] = mapped_column(_TZ)
    deadline: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    completed_local_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    routine_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("routines.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))
