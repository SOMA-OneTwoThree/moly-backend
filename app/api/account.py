"""계정 API — 온보딩·프로필. 전 엔드포인트 Bearer 인증(get_current_user)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.account import OnboardingRequest, PatchMeRequest
from app.services import account as account_service

router = APIRouter(tags=["account"])


@router.post("/onboarding")
async def onboarding(
    req: OnboardingRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await account_service.onboarding(session, user_id, req)


@router.get("/me")
async def get_me(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await account_service.get_me(session, user_id)


@router.patch("/me")
async def patch_me(
    req: PatchMeRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await account_service.patch_me(session, user_id, req)
