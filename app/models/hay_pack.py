"""hay_packs — 건초 IAP 상품 목록(ERD §4.5). 읽기 전용(충전소에 노출)."""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class HayPack(Base):
    __tablename__ = "hay_packs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    app_store_product_id: Mapped[str] = mapped_column(String, unique=True)
    hay_amount: Mapped[int] = mapped_column(Integer)
    price_krw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
