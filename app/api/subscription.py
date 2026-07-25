"""구독 API — RevenueCat 기반. 조회는 인증, RC 웹훅은 Authorization 헤더 값으로 인증.

구독·건초 IAP 검증은 RevenueCat이 대행 → 클라는 RC SDK 사용, 백엔드는 RC 웹훅으로만 동기.
(직접 StoreKit verify/restore·ASSN·wallet 경로는 RC 전환으로 제거됨.)
"""
from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.common import StatusResponse
from app.schemas.subscription import (
    RevenueCatWebhook,
    SubscriptionPlansResponse,
    SubscriptionResponse,
)
from app.services import subscription

_log = logging.getLogger("moly-backend")

router = APIRouter(tags=["subscription"])


@router.get("/subscription", response_model=SubscriptionResponse)
async def get_subscription(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await subscription.get_subscription(session, user_id)


@router.get("/subscription/plans", response_model=SubscriptionPlansResponse)
async def get_plans(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await subscription.get_plans(session, user_id)


@router.post("/webhooks/revenuecat", response_model=StatusResponse)
async def revenuecat_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """RevenueCat 웹훅. 인증 = 대시보드에 설정한 Authorization 헤더 값 일치(상수시간 비교).

    미설정/불일치 = 401(fail-closed) — body는 인증 후에만 파싱한다(깨진 JSON도 미인증이면
    401). 본문 {api_version, event:{type,...}} 형태 위반 = 422(RC가 실패로 기록·재시도).
    event 내부는 RC가 field를 수시 추가하므로 type 외 강제하지 않는다.
    """
    expected = settings.revenuecat_webhook_auth
    if not expected or not authorization or not hmac.compare_digest(authorization, expected):
        raise errors.unauthorized("웹훅 인증에 실패했어요.")
    try:
        body = RevenueCatWebhook.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise errors.validation("RevenueCat 웹훅 본문 형식이 올바르지 않습니다.")
    if body.api_version != "1.0":
        _log.warning("RC 웹훅: 예상 밖 api_version(%r) — 계약 확인 필요", body.api_version)
    await subscription.handle_revenuecat_event(session, body.event.model_dump())
    return {"status": "ok"}
