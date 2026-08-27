"""오늘의 운세 API — 프로필, 당일 공개, 무료 사용자 광고 세션."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.fortune import (
    DailyFortuneRevealResponse,
    DailyFortuneStatusResponse,
    FortuneAdSessionRequest,
    FortuneAdSessionResponse,
    Locale,
    FortuneProfilePut,
    FortuneProfilePutResponse,
    FortuneProfileResponse,
)
from app.services import fortune

router = APIRouter(tags=["fortune"])


@router.get("/fortune-profile", response_model=FortuneProfileResponse)
async def get_fortune_profile(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await fortune.get_profile(session, user_id)


@router.put("/fortune-profile", response_model=FortuneProfilePutResponse)
async def put_fortune_profile(
    body: FortuneProfilePut,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await fortune.put_profile(session, user_id, body)


@router.delete("/fortune-profile", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fortune_profile(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await fortune.delete_profile(session, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/daily-fortune/status",
    response_model=DailyFortuneStatusResponse,
    response_model_exclude_none=True,
)
async def get_daily_fortune_status(
    locale: Locale | None = Header(default=None, alias="X-App-Locale"),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await fortune.status(session, user_id, locale=locale)


@router.post(
    "/daily-fortune/reveal",
    response_model=DailyFortuneRevealResponse,
    response_model_exclude_none=True,
)
async def reveal_daily_fortune(
    locale: Locale | None = Header(default=None, alias="X-App-Locale"),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await fortune.reveal(session, user_id, locale=locale)


@router.post(
    "/daily-fortune/ad-sessions",
    response_model=FortuneAdSessionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": FortuneAdSessionResponse}},
)
async def create_fortune_ad_session(
    body: FortuneAdSessionRequest,
    response: Response,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    result, created = await fortune.create_ad_session(
        session, user_id, client_request_id=body.client_request_id
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return result
