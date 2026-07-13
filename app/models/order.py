"""orders / order_items — 모든 구매의 단일 진입점(DB_REFACTOR §B.2).

currency: KRW(IAP 건초, 실결제) | HAY(상점 꾸미기, 재화 차감 — 트랜잭션 안에서 즉시 paid).
order_items.unit_price = 구매 시점 가격 스냅샷(가격정책 변동·부분환불 대비).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    currency: Mapped[str] = mapped_column(String)  # KRW | HAY
    status: Mapped[str] = mapped_column(String)  # pending | paid | failed | refunded
    total_amount: Mapped[int] = mapped_column(Integer)  # KRW 결제금액 또는 HAY 차감량(양수)
    created_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    quantity: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    unit_price: Mapped[int] = mapped_column(Integer)  # 구매 시점 가격 스냅샷
