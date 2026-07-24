"""payments — 실결제(현금) 기록(ERD §4.7).

IAP 건초 = order_id 연결(1:1) / 구독 결제(구매·갱신) = subscription_id 연결.
store_transaction_id UNIQUE = 영수증 멱등 키. 매출 집계는 이 테이블 단일 소스.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, text
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
    store: Mapped[str] = mapped_column(String)  # 실제 스토어(app_store|play_store|…). 기록 시 항상 명시
    store_transaction_id: Mapped[str] = mapped_column(String, unique=True)
    # 결제금액(원통화·무손실 numeric). 이벤트에 없으면 NULL.
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    # 구매 통화(ISO 4217). 미확인이면 NULL — KRW로 확정하지 않는다.
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)  # paid | refunded
    paid_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
