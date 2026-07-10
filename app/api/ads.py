"""광고 API — 세션 발급(인증) + SSV 웹훅(공개, 서명검증 후 자동 지급)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.db import get_session
from app.core.security import get_current_user
from app.services import ads, ads_ssv

router = APIRouter(tags=["ads"])


@router.post("/reward-ad-sessions")
async def create_reward_ad_session(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """광고 시청 전 세션 발급 — 오늘 한도 확인 후 SSV에 실을 값 반환. 초과 = 429."""
    return await ads.create_session(session, user_id)


@router.get("/webhooks/ad-ssv")
async def ad_ssv(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """AdMob 리워드 SSV 콜백(서버-서버). 인증 = 서명. 검증 후 세션으로 자동 +10 지급."""
    p = request.query_params
    key_id, signature = p.get("key_id"), p.get("signature")
    reward_session_id, transaction_id = p.get("custom_data"), p.get("transaction_id")
    if not (key_id and signature and reward_session_id and transaction_id):
        raise errors.validation("SSV 파라미터가 누락됐어요.")
    if not await ads_ssv.verify(request.url.query, key_id, signature):
        raise errors.ad_verify_failed()
    await ads.grant_from_ssv(session, reward_session_id, transaction_id)
    return {"status": "ok"}
