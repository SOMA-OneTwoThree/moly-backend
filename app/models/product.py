"""products — 판매 상품 카탈로그(ERD §4.5). order_items가 가리키는 단일 상품 FK.

product_type: hay_pack(IAP 건초, KRW 실결제) | cosmetic(꾸미기, HAY 결제).
타입별 컬럼 상호 강제는 DB CHECK(products_hay_pack_ck / products_cosmetic_ck).
"""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    product_type: Mapped[str] = mapped_column(String)  # hay_pack | cosmetic
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    # cosmetic 전용. id는 내부 FK용 UUID, public_id는 API에 노출하는 안정 식별자다.
    public_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    slot: Mapped[str | None] = mapped_column(String, nullable=True)  # theme | head | neck | body
    price_hay: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL = 구매 불가
    is_subscriber_only: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    asset_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assets: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # hay_pack 전용
    hay_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_krw: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 표시 참고용(결제가는 StoreKit)
    app_store_product_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
