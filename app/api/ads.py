"""광고 API — SSV 웹훅(공개, 서명검증) + 보상 수령(인증)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.ads import AdRewardRequest
from app.services import ads, ads_ssv

router = APIRouter(tags=["ads"])


@router.get("/webhooks/ad-ssv")
async def ad_ssv(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """AdMob 리워드 SSV 콜백(서버-서버, 서명 검증). 인증 없음 — 서명이 인증."""
    p = request.query_params
    key_id, signature = p.get("key_id"), p.get("signature")
    user_id, transaction_id = p.get("custom_data"), p.get("transaction_id")
    if not (key_id and signature and user_id and transaction_id):
        raise errors.validation("SSV 파라미터가 누락됐어요.")
    if not await ads_ssv.verify(request.url.query, key_id, signature):
        raise errors.ad_verify_failed()
    await ads.record_ssv(session, user_id, transaction_id)
    return {"status": "ok"}


@router.post("/ads/reward")
async def ad_reward(
    req: AdRewardRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await ads.claim(session, user_id, req.ssv_transaction_id)
