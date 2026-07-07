"""subscriptions — App Store가 갱신하는 서버 원본(ERD §4.3). enum은 String 매핑."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)  # ERD timestamptz — aware datetime 강제


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    plan: Mapped[str] = mapped_column(String)  # monthly | yearly
    status: Mapped[str] = mapped_column(String)  # active | grace_period | expired | revoked
    original_transaction_id: Mapped[str] = mapped_column(String, unique=True)
    latest_transaction_id: Mapped[str | None] = mapped_column(String, nullable=True)
    purchased_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    auto_renew_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    environment: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
