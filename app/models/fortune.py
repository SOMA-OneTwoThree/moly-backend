"""오늘의 운세 프로필·당일 결과·광고 검증 세션."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class FortuneProfile(Base):
    __tablename__ = "fortune_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    gender: Mapped[str] = mapped_column(String, nullable=False)
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class DailyFortune(Base):
    __tablename__ = "daily_fortunes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fortune_profiles.user_id", ondelete="CASCADE"), primary_key=True
    )
    fortune_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone_snapshot: Mapped[str] = mapped_column(String, nullable=False)
    profile_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    semantic_result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    copy_by_locale: Mapped[dict] = mapped_column(JSONB, nullable=False)
    unlock_state: Mapped[str] = mapped_column(String, nullable=False)
    unlock_source: Mapped[str | None] = mapped_column(String, nullable=True)
    unlocked_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    revealed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    ephemeris_version: Mapped[str] = mapped_column(String, nullable=False)
    rule_version: Mapped[str] = mapped_column(String, nullable=False)
    copy_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class FortuneAdSession(Base):
    __tablename__ = "fortune_ad_sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fortune_profiles.user_id", ondelete="CASCADE"), nullable=False
    )
    fortune_date: Mapped[date] = mapped_column(Date, nullable=False)
    client_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ssv_transaction_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
