"""건초 현금구매(IAP consumable) — RevenueCat NON_RENEWING_PURCHASE 이벤트로 지급.

RC가 영수증 검증 대행 → 우리는 event.product_id/transaction_id만 신뢰(웹훅 인증이 신뢰경계).
store_transaction_id로 멱등. 지급 = Order(KRW,paid) + OrderItem + Payment + 원장 한 묶음.
커밋은 호출측(RC 웹훅 핸들러)이 한다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

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


async def grant_pack(
    session: AsyncSession,
    uid,
    product_id: str,
    transaction_id: str,
    *,
    store: str,
    amount: Decimal | None = None,
    currency: str | None = None,
) -> None:
    """건초팩 지급(멱등: store_transaction_id). 미상 상품/중복/누락은 조용히 스킵. 커밋 안 함.

    store = RC가 알려준 실제 스토어(app_store|play_store|…).
    amount/currency = RC가 알려준 실제 결제 금액·통화(해외 결제 대응). 이벤트에 없으면
    국내 카탈로그가(price_krw·KRW)로 폴백. payments는 매출 단일 소스라 실통화가 남아야 한다.
    """
    if not (product_id and transaction_id):
        return
    if await payment_exists(session, transaction_id):
        return  # 멱등 — 이미 지급된 거래
    # 스토어에 맞는 상품ID 컬럼으로 조회(Google Play는 play_store_product_id).
    id_col = (
        Product.play_store_product_id if store == "play_store" else Product.app_store_product_id
    )
    product = (
        await session.execute(
            select(Product).where(id_col == product_id, Product.product_type == "hay_pack")
        )
    ).scalars().first()
    if product is None:
        _log.warning("RC IAP: 미상 상품 %s (store=%s) — 스킵", product_id, store)
        return
    # Order = 국내 카탈로그 스냅샷(KRW 표시가). 실결제 통화/금액은 아래 Payment가 권위(매출 단일 소스).
    ord_ = order_service.create_paid_order(
        session, uid, currency="KRW", product=product, unit_price=product.price_krw or 0
    )
    await hay_ledger.apply(session, uid, "iap_purchase", product.hay_amount, order_id=ord_.id)
    session.add(
        Payment(
            user_id=uid, order_id=ord_.id, store=store, store_transaction_id=transaction_id,
            amount=amount if amount is not None else product.price_krw,
            currency=currency if currency is not None else "KRW",
            status="paid", paid_at=datetime.now(timezone.utc),
        )
    )
