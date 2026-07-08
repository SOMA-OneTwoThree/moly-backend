"""건초 현금구매(IAP consumable) — StoreKit JWS 검증 → 건초 지급. transaction_id 멱등."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.hay_pack import HayPack
from app.models.iap_purchase import IapPurchase
from app.services import app_store, hay_ledger
from app.services.account import _uid


async def purchase(session: AsyncSession, user_id: str, signed_transaction: str) -> dict[str, Any]:
    uid = _uid(user_id)
    payload = app_store.decode_transaction(signed_transaction)
    product_id = payload.get("productId")
    transaction_id = str(payload.get("transactionId"))

    existing = (
        await session.execute(
            select(IapPurchase).where(IapPurchase.transaction_id == transaction_id)
        )
    ).scalars().first()
    if existing is not None:
        raise errors.already_processed()  # 409 — 영수증 중복

    pack = (
        await session.execute(
            select(HayPack).where(HayPack.app_store_product_id == product_id)
        )
    ).scalars().first()
    if pack is None:
        raise errors.receipt_invalid()  # 422 — 미상 상품

    balance = await hay_ledger.apply(
        session, uid, "iap_purchase", pack.hay_amount, ref_id=transaction_id
    )
    session.add(
        IapPurchase(
            user_id=uid, hay_pack_id=pack.id, transaction_id=transaction_id,
            status="verified", purchased_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return {"amount": pack.hay_amount, "balance_after": balance}
