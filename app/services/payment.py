"""건초 현금구매(IAP consumable) — RevenueCat NON_RENEWING_PURCHASE 이벤트로 지급.

RC가 영수증 검증 대행 → 우리는 event.product_id/transaction_id만 신뢰(웹훅 인증이 신뢰경계).
store_transaction_id로 멱등. 지급 = Order(KRW,paid) + OrderItem + Payment + 원장 한 묶음.
커밋은 호출측(RC 웹훅 핸들러)이 한다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment
from app.models.product import Product
from app.services import hay_ledger
from app.services import order as order_service

_log = logging.getLogger("moly-backend")


async def payment_exists(session: AsyncSession, store_transaction_id: str) -> bool:
    row = await session.execute(
        select(Payment.id).where(Payment.store_transaction_id == store_transaction_id)
    )
    return row.scalars().first() is not None


async def grant_pack(session: AsyncSession, uid, product_id: str, transaction_id: str) -> None:
    """건초팩 지급(멱등: store_transaction_id). 미상 상품/중복/누락은 조용히 스킵. 커밋 안 함."""
    if not (product_id and transaction_id):
        return
    if await payment_exists(session, transaction_id):
        return  # 멱등 — 이미 지급된 거래
    product = (
        await session.execute(
            select(Product).where(
                Product.app_store_product_id == product_id,
                Product.product_type == "hay_pack",
            )
        )
    ).scalars().first()
    if product is None:
        _log.warning("RC IAP: 미상 상품 %s — 스킵", product_id)
        return
    ord_ = order_service.create_paid_order(
        session, uid, currency="KRW", product=product, unit_price=product.price_krw or 0
    )
    await hay_ledger.apply(session, uid, "iap_purchase", product.hay_amount, order_id=ord_.id)
    session.add(
        Payment(
            user_id=uid, order_id=ord_.id, store_transaction_id=transaction_id,
            amount=product.price_krw, currency="KRW", status="paid",
            paid_at=datetime.now(timezone.utc),
        )
    )
