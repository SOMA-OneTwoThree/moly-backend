"""shop_items / user_items — 상점 상품·인벤토리(ERD §4.7·4.8). 장착은 user_equipment."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class ShopItem(Base):
    __tablename__ = "shop_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    slot: Mapped[str] = mapped_column(String)  # background | head | neck | body
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    price_hay: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL = 구독 전용 비매품
    is_subscriber_only: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    assets: Mapped[dict] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class UserItem(Base):
    __tablename__ = "user_items"
    __table_args__ = (UniqueConstraint("user_id", "shop_item_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    shop_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    hay_transaction_id: Mapped[int | None] = mapped_column(nullable=True)  # 구매 차감 원장(무상=NULL)
    acquired_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
