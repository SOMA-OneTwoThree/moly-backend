"""user_items — 보유 + 장착 상태(ERD §4.8).

equipped_slot NULL = 미장착. 슬롯당 1개 장착은 부분 UNIQUE, 슬롯 일치는 복합 FK가 DB 강제.
source: purchase(주문 구매) | subscription(구독 전용 장착용 — 소유 아님, 인벤토리 미노출)
      | admin_grant(운영 무상 지급).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class UserItem(Base):
    __tablename__ = "user_items"
    __table_args__ = (UniqueConstraint("user_id", "product_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(String, default="purchase", server_default=text("'purchase'"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    equipped_slot: Mapped[str | None] = mapped_column(String, nullable=True)  # NULL = 미장착
    equipped_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    acquired_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
