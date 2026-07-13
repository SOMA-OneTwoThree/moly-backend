"""payments — 실결제(현금) 기록(ERD §4.7).

IAP 건초 = order_id 연결(1:1) / 구독 결제(구매·갱신) = subscription_id 연결.
store_transaction_id UNIQUE = 영수증 멱등 키. 매출 집계는 이 테이블 단일 소스.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    store: Mapped[str] = mapped_column(String, default="app_store", server_default=text("'app_store'"))
    store_transaction_id: Mapped[str] = mapped_column(String, unique=True)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 결제금액. 이벤트에 없으면 NULL
    currency: Mapped[str] = mapped_column(String, default="KRW", server_default=text("'KRW'"))
    status: Mapped[str] = mapped_column(String)  # paid | refunded
    paid_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
