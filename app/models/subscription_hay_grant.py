"""subscription_hay_grants — 구독 건초 증정 이력(ERD §4.4). (user,plan) UNIQUE로 최초1회 강제."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SubscriptionHayGrant(Base):
    __tablename__ = "subscription_hay_grants"
    __table_args__ = (UniqueConstraint("user_id", "plan"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    plan: Mapped[str] = mapped_column(String)  # monthly | yearly
    hay_transaction_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    granted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
