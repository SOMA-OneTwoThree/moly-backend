"""구독 API — RevenueCat 기반. 조회는 인증, RC 웹훅은 Authorization 헤더 값으로 인증.

구독·건초 IAP 검증은 RevenueCat이 대행 → 클라는 RC SDK 사용, 백엔드는 RC 웹훅으로만 동기.
(직접 StoreKit verify/restore·ASSN·wallet 경로는 RC 전환으로 제거됨.)
"""
from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Body, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.common import StatusResponse
from app.schemas.subscription import SubscriptionPlansResponse, SubscriptionResponse
from app.services import subscription

router = APIRouter(tags=["subscription"])


@router.get("/subscription", response_model=SubscriptionResponse)
async def get_subscription(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await subscription.get_subscription(session, user_id)


@router.get("/subscription/plans", response_model=SubscriptionPlansResponse)
async def get_plans(
    _user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    return subscription.get_plans()


@router.post("/webhooks/revenuecat", response_model=StatusResponse)
async def revenuecat_webhook(
    payload: dict = Body(...),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """RevenueCat 웹훅. 인증 = 대시보드에 설정한 Authorization 헤더 값 일치(상수시간 비교).

    본문 {api_version, event:{...}}. 미설정/불일치 = 401(fail-closed).
    """
    expected = settings.revenuecat_webhook_auth
    if not expected or not authorization or not hmac.compare_digest(authorization, expected):
        raise errors.unauthorized("웹훅 인증에 실패했어요.")
    event = payload.get("event")
    if isinstance(event, dict):
        await subscription.handle_revenuecat_event(session, event)
    return {"status": "ok"}
