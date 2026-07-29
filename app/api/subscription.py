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

_MAX_WEBHOOK_BYTES = 256 * 1024  # RC 웹훅 본문 상한(과대 payload 거부 — inbox JSONB 내구 보호)
_MAX_EVENT_ID_LEN = 255          # event.id는 inbox PK(B-tree) — 과대 id는 INSERT 전 거절


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
    # 본문은 인증 후에만 읽는다(fail-closed 유지). 과대 payload는 파싱 전 거부(inbox JSONB 내구 보호).
    raw = await request.body()
    if len(raw) > _MAX_WEBHOOK_BYTES:
        raise errors.validation("RevenueCat 웹훅 본문이 너무 큽니다.")
    try:
        body = RevenueCatWebhook.model_validate_json(raw)
    except (ValueError, ValidationError):
        raise errors.validation("RevenueCat 웹훅 본문 형식이 올바르지 않습니다.")
    if body.api_version != "1.0":
        _log.warning("RC 웹훅: 예상 밖 api_version(%r) — 계약 확인 필요", body.api_version)
    event = body.event.model_dump()
    # event.id = RC 전역 유일 식별자(공식). inbox PK로 중복 웹훅 멱등·durable 영속(SOMA-372).
    # PK B-tree라 과대 id는 INSERT 시 인덱스 실패로 내구가 깨진다 — 저장 전 길이 검증(RC id는 UUID 수준).
    event_id = str(event.get("id") or "")
    if not event_id or len(event_id) > _MAX_EVENT_ID_LEN:
        raise errors.validation("RevenueCat 이벤트 id가 없거나 너무 깁니다.")
    # raw를 inbox에 커밋 후 동기 소비 — 처리 성패와 무관히 항상 200(재처리는 워커·재요약).
    await subscription.ingest_event(session, event_id, event)
    return {"status": "ok"}
