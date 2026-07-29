"""건초 현금구매(IAP consumable) — RevenueCat NON_RENEWING_PURCHASE 이벤트로 지급.

RC가 영수증 검증 대행 → 우리는 event.product_id/transaction_id만 신뢰(웹훅 인증이 신뢰경계).
store_transaction_id로 멱등. 지급 = Order(KRW,paid) + OrderItem + Payment + 원장 한 묶음.
커밋은 호출측(RC 웹훅 핸들러)이 한다.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hay_transaction import HayTransaction
from app.models.order import OrderItem
from app.models.payment import Payment
from app.models.product import Product
from app.services import hay_ledger
from app.services import order as order_service

_log = logging.getLogger("moly-backend")


def validate_payment_target(
    order_id: uuid.UUID | None, subscription_id: uuid.UUID | None
) -> str:
    """payments_target_ck(OR)를 런타임에서 XOR로 강화 — 정확히 하나만 설정. 위반 시 ValueError.

    모든 Payment 생성·환불·복구 진입점 공통(SOMA-372 §11.5). 양쪽 다 or 양쪽 NULL이면 실오류
    (OR 제약은 양쪽 설정을 못 막고, 폴백 조회가 다른 거래를 오환불할 위험). 반환 = 'order' | 'subscription'.
    """
    has_order = order_id is not None
    has_sub = subscription_id is not None
    if has_order == has_sub:  # 양쪽 다 or 양쪽 NULL
        raise ValueError(
            f"payment target XOR 위반: order_id={order_id!r} subscription_id={subscription_id!r}"
        )
    return "order" if has_order else "subscription"


async def pack_ledger_amount(session: AsyncSession, order_id: uuid.UUID) -> int:
    """건초팩 주문의 실제 지급 원장액 = sum(iap_purchase where order_id). 상수·카탈로그 재조회 금지.

    회수·복구량 권위(SOMA-372 §4). refund_revoke(음수)는 type이 달라 이 합에 안 섞인다(원 지급액 불변).
    """
    total = (
        await session.execute(
            select(func.coalesce(func.sum(HayTransaction.amount), 0)).where(
                HayTransaction.order_id == order_id,
                HayTransaction.type == "iap_purchase",
            )
        )
    ).scalar_one()
    return int(total)


async def restore_pack(
    session: AsyncSession, user_id: uuid.UUID, order_id: uuid.UUID
) -> int:
    """환불 복구(REFUND_REVERSED) — 원 주문 iap_purchase 합계를 양수로 직접 재지급. 반환 = 복구액.

    grant_pack은 store_transaction_id 멱등으로 조기반환하므로 재사용 불가 → 원장 직접 기록.
    커밋 안 함(호출측=inbox process_event가 트랜잭션 경계).
    """
    intended = await pack_ledger_amount(session, order_id)
    if intended <= 0:
        return 0
    await hay_ledger.apply(
        session, user_id, "admin_adjustment", intended, order_id=order_id, allow_negative=True
    )
    return intended


async def payment_exists(session: AsyncSession, store_transaction_id: str) -> bool:
    row = await session.execute(
        select(Payment.id).where(Payment.store_transaction_id == store_transaction_id)
    )
    return row.scalars().first() is not None


async def _payment_by_tx(session: AsyncSession, store_transaction_id: str) -> Payment | None:
    return (
        await session.execute(
            select(Payment).where(Payment.store_transaction_id == store_transaction_id)
        )
    ).scalars().first()


async def _order_item_product_id(session: AsyncSession, order_id: uuid.UUID) -> uuid.UUID | None:
    """주문의 상품 FK(order_items.product_id) — 멱등 재검증용(거래ID가 다른 상품에 재사용됐는지 대조).
    단건 주문 전제(create_paid_order는 항목 1건)."""
    return (
        await session.execute(
            select(OrderItem.product_id).where(OrderItem.order_id == order_id)
        )
    ).scalars().first()


async def grant_pack(
    session: AsyncSession,
    uid,
    product_id: str,
    transaction_id: str,
    *,
    store: str,
    amount: Decimal | None = None,
    currency: str | None = None,
) -> bool:
    """건초팩 지급(멱등: store_transaction_id). 반환 = 지급/멱등중복이면 True, 실패면 False.

    반환값(SOMA-372 §11.3, 은폐 금지): 정상 지급·중복(멱등)은 True. 식별자 누락·미등록 상품은
    False로 알려 상위(RC 핸들러)가 permanent_failure로 관측한다(건초 미지급 IAP가 묻히지 않게).
    store = RC가 알려준 실제 스토어(app_store|play_store|…).
    amount/currency = RC가 알려준 실제 결제 금액·통화(해외 결제 대응). 이벤트에 없으면
    국내 카탈로그가(price_krw·KRW)로 폴백. payments는 매출 단일 소스라 실통화가 남아야 한다.
    """
    if not (product_id and transaction_id):
        return False  # 식별자 누락 — 상위에서 permanent_failure
    # transaction_id는 원문 그대로 저장·조회(truncate 금지) — store_transaction_id는 text라 무제한.
    # 과대 입력 거절은 상위(_dispatch의 _txn_id)가 처리한다(저장·환불조회 ID 통일, SOMA-372 회귀 방지).
    # 사전 재조회 의미검증(§6) — payment_exists 조기반환은 다른 user/product/order가 같은
    # transaction_id를 재사용해도 멱등으로 오인해, 건초 지급 없이 processed로 묻었다.
    existing = await _payment_by_tx(session, transaction_id)
    # 명백한 거래ID 오용(다른 유저 or 팩 결제 아님=order_id 없거나 subscription_id 있음)은 상품 조회 전
    # 즉시 거절(XOR: 팩 결제는 order_id만 set).
    if existing is not None and not (
        existing.user_id == uid
        and existing.order_id is not None
        and existing.subscription_id is None
    ):
        _log.warning(
            "RC IAP: 거래ID 오용 tx=%s 기존 user=%s order=%s sub=%s 요청 user=%s — 스킵",
            transaction_id, existing.user_id, existing.order_id, existing.subscription_id, uid,
        )
        return False  # 거래ID 오용 — 상위에서 permanent_failure
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
        return False  # 미등록 상품 — 상위에서 permanent_failure(건초 미지급 관측)
    if existing is not None:
        # 같은 유저·팩 결제 확인됨 — 기존 주문 상품이 이번 RC 상품과 일치해야 멱등(§6). 불일치면 거래ID가
        # 다른 상품에 재사용된 것이라 지급 없이 permanent_failure로 관측한다.
        existing_pid = await _order_item_product_id(session, existing.order_id)
        if existing_pid != product.id:
            _log.warning(
                "RC IAP: 거래ID 상품 불일치 tx=%s 기존상품=%s 요청상품=%s(%s) — 스킵",
                transaction_id, existing_pid, product.id, product_id,
            )
            return False  # 상품 불일치 — 상위에서 permanent_failure
        return True  # 멱등 — 같은 유저·같은 상품 팩 결제
    # Order = 국내 카탈로그 스냅샷(KRW 표시가). 실결제 통화/금액은 아래 Payment가 권위(매출 단일 소스).
    ord_ = order_service.create_paid_order(
        session, uid, currency="KRW", product=product, unit_price=product.price_krw or 0
    )
    await hay_ledger.apply(session, uid, "iap_purchase", product.hay_amount, order_id=ord_.id)
    validate_payment_target(order_id=ord_.id, subscription_id=None)  # 진입점 공통 XOR 검증
    session.add(
        Payment(
            user_id=uid, order_id=ord_.id, store=store, store_transaction_id=transaction_id,
            amount=amount if amount is not None else product.price_krw,
            currency=currency if currency is not None else "KRW",
            status="paid", paid_at=datetime.now(timezone.utc),
        )
    )
    return True
