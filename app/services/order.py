"""주문 — 모든 구매(KRW 건초 IAP · HAY 상점 꾸미기)의 공통 진입점(DB_REFACTOR §B.2).

MVP 주문은 단건·즉시확정이라 pending 단계 없이 paid로 생성(HAY는 원장 차감과 한 트랜잭션,
KRW는 RC가 결제를 이미 확정한 웹훅 시점). 커밋은 호출측.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.product import Product


def create_paid_order(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    currency: str,
    product: Product,
    unit_price: int,
    quantity: int = 1,
) -> Order:
    """paid 주문 + 주문 항목 생성(단건). unit_price = 구매 시점 가격 스냅샷."""
    # id 명시 생성 — 원장·결제·인벤토리가 flush 전에 order.id를 참조(컬럼 default는 flush 시점)
    order = Order(
        id=uuid.uuid4(), user_id=user_id, currency=currency, status="paid",
        total_amount=unit_price * quantity,
    )
    session.add(order)
    session.add(
        OrderItem(order_id=order.id, product_id=product.id, quantity=quantity, unit_price=unit_price)
    )
    return order
