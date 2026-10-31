"""오늘의 작은 실천 — 공용 행동 풀과 사용자별 하루 제안."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class DailyPracticeAction(Base):
    __tablename__ = "daily_practice_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    category: Mapped[str] = mapped_column(Text)
    slots: Mapped[list[str]] = mapped_column(ARRAY(Text))
    icon: Mapped[str] = mapped_column(Text)
    title_ko: Mapped[str] = mapped_column(Text, unique=True)
    title_en: Mapped[str] = mapped_column(Text)
    title_ja: Mapped[str] = mapped_column(Text)
    speech_ko: Mapped[str] = mapped_column(Text)
    speech_en: Mapped[str] = mapped_column(Text)
    speech_ja: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    source: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))


class DailyPracticeOffer(Base):
    __tablename__ = "daily_practice_offers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE")
    )
    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("daily_practice_actions.id")
    )
    local_date: Mapped[date] = mapped_column(Date)
    expires_at: Mapped[datetime] = mapped_column(_TZ)
    status: Mapped[str] = mapped_column(Text, server_default=text("'offered'"))
    routine_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("routines.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_TZ, server_default=text("now()"))
