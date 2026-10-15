"""루틴 테이블(ERD §5.5) — routines·completions·요일 이력·스킵·템플릿. 주기 = 요일별. 삭제 = soft delete."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class Routine(Base):
    __tablename__ = "routines"
    __table_args__ = (UniqueConstraint("user_id", "id", name="routines_user_id_id_uq"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String)  # 유저 편집 이름. 기본 루틴은 name_i18n로 다국어.
    # 기본 루틴 다국어({"ko":..,"en":..,"ja":..}). 유저가 이름 편집 시 NULL로 클리어(SOMA-346).
    # none_as_null: None 대입이 jsonb 'null'로 저장되면 obj_ck CHECK 위반(SOMA-앱 500).
    name_i18n: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    frequency_per_week: Mapped[int] = mapped_column(SmallInteger)  # 항상 len(days_of_week). 응답 하위호환용
    days_of_week: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger))  # 지정 요일(ISO 1=월…7=일)
    reminder_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reminder_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)  # soft delete
    created_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
    icon: Mapped[str] = mapped_column(String, server_default=text("'seedling'"))
    color: Mapped[str] = mapped_column(String, server_default=text("'peach'"))
    template_id: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class RoutineCompletion(Base):
    __tablename__ = "routine_completions"
    __table_args__ = (
        UniqueConstraint("routine_id", "activity_date"),
        ForeignKeyConstraint(
            ["user_id", "routine_id"],
            ["routines.user_id", "routines.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    routine_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    activity_date: Mapped[date] = mapped_column(Date)
    completed_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)


class RoutineSchedule(Base):
    __tablename__ = "routine_schedules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "routine_id"],
            ["routines.user_id", "routines.id"],
            ondelete="CASCADE",
        ),
    )

    routine_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    effective_from: Mapped[date] = mapped_column(Date, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    days_of_week: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger))
    created_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)


class RoutineSkip(Base):
    __tablename__ = "routine_skips"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "routine_id"],
            ["routines.user_id", "routines.id"],
            ondelete="CASCADE",
        ),
        Index("routine_skips_user_date_idx", "user_id", "activity_date"),
    )

    routine_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    activity_date: Mapped[date] = mapped_column(Date, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)


class RoutineTemplateCategory(Base):
    __tablename__ = "routine_template_categories"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name_i18n: Mapped[dict] = mapped_column(JSONB)
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class RoutineTemplate(Base):
    __tablename__ = "routine_templates"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    category_id: Mapped[str] = mapped_column(String, ForeignKey("routine_template_categories.id"))
    name_i18n: Mapped[dict] = mapped_column(JSONB)
    icon: Mapped[str] = mapped_column(String)
    color: Mapped[str] = mapped_column(String)
    days_of_week: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger))
    is_recommended: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
