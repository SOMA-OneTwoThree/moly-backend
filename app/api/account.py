"""계정 API — 온보딩·프로필. 전 엔드포인트 Bearer 인증(get_current_user)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.account import (
    LogoutRequest,
    NotificationsPatchRequest,
    OnboardingRequest,
    PatchMeRequest,
    PushTokenRequest,
)
from app.services import account as account_service
from app.services import account_settings as settings_service

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


@router.get("/me/notifications")
async def get_notifications(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    return await settings_service.get_notifications(session, user_id)


@router.patch("/me/notifications")
async def patch_notifications(
    req: NotificationsPatchRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    return await settings_service.patch_notifications(session, user_id, req)


@router.post("/me/push-token", status_code=status.HTTP_204_NO_CONTENT)
async def register_push_token(
    req: PushTokenRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await settings_service.register_push_token(session, user_id, req)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    req: LogoutRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await settings_service.logout_device(session, user_id, req.push_token)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await settings_service.delete_account(session, user_id)
