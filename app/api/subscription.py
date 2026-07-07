"""구독·IAP API. 조회/검증은 인증, ASSN 웹훅은 공개(서명이 인증)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.subscription import IapPurchaseRequest, RestoreRequest, VerifyRequest
from app.services import iap, subscription

router = APIRouter(tags=["subscription"])


@router.get("/subscription")
async def get_subscription(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await subscription.get_subscription(session, user_id)


@router.get("/subscription/plans")
async def get_plans() -> dict[str, Any]:
    return subscription.get_plans()


@router.post("/subscription/verify")
async def verify(
    req: VerifyRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await subscription.verify(session, user_id, req.signed_transaction)


@router.post("/subscription/restore")
async def restore(
    req: RestoreRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await subscription.restore(session, user_id, req.signed_transactions)


@router.post("/webhooks/appstore")
async def appstore_webhook(
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Apple ASSN v2(서버-서버). 본문 {signedPayload}. 인증 없음 — 서명이 인증."""
    signed = payload.get("signedPayload")
    if signed:
        await subscription.handle_webhook(session, signed)
    return {"status": "ok"}


@router.post("/wallet/purchases")
async def wallet_purchase(
    req: IapPurchaseRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await iap.purchase(session, user_id, req.signed_transaction)
