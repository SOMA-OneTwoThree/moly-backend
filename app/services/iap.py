"""건초 현금구매(IAP consumable) — RevenueCat NON_RENEWING_PURCHASE 이벤트로 지급.

RC가 영수증 검증 대행 → 우리는 event.product_id/transaction_id만 신뢰(웹훅 인증이 신뢰경계).
transaction_id로 멱등. 커밋은 호출측(RC 웹훅 핸들러)이 한다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hay_pack import HayPack
from app.models.iap_purchase import IapPurchase
from app.services import hay_ledger

_log = logging.getLogger("moly-backend")


async def grant_pack(session: AsyncSession, uid, product_id: str, transaction_id: str) -> None:
    """건초팩 지급(멱등: transaction_id). 미상 상품/중복/누락은 조용히 스킵. 커밋 안 함."""
    if not (product_id and transaction_id):
        return
    existing = (
        await session.execute(
            select(IapPurchase).where(IapPurchase.transaction_id == transaction_id)
        )
    ).scalars().first()
    if existing is not None:
        return  # 멱등 — 이미 지급된 거래
    pack = (
        await session.execute(
            select(HayPack).where(HayPack.app_store_product_id == product_id)
        )
    ).scalars().first()
    if pack is None:
        _log.warning("RC IAP: 미상 상품 %s — 스킵", product_id)
        return
    tx = await hay_ledger.apply(
        session, uid, "iap_purchase", pack.hay_amount, ref_id=transaction_id
    )
    session.add(
        IapPurchase(
            user_id=uid, hay_pack_id=pack.id, transaction_id=transaction_id,
            status="verified", hay_transaction_id=tx.id,
            purchased_at=datetime.now(timezone.utc),
        )
    )
