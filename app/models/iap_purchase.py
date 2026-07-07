"""iap_purchases — 건초 IAP 결제 기록(ERD §4.6). transaction_id UNIQUE로 중복지급 방지."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class IapPurchase(Base):
    __tablename__ = "iap_purchases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    hay_pack_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    transaction_id: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String)  # pending | verified | failed | refunded
    hay_transaction_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
