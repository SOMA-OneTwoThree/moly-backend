"""오늘의 글귀 API — 현지 날짜별 공용 문장 조회와 당일 확인 기록."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.affirmation import (
    DailyAffirmationAcknowledgeRequest,
    DailyAffirmationAcknowledgeResponse,
    DailyAffirmationResponse,
)
from app.services import affirmation

router = APIRouter(tags=["affirmation"])

_LOCALE_HEADER_DESCRIPTION = (
    "글귀 언어. ko, en, ja 또는 해당 언어의 지역 태그(예: ko-KR, en-US, ja-JP)를 받으며 "
    "미설정·미지원 언어는 en으로 폴백한다."
)
_TIMEZONE_HEADER_DESCRIPTION = (
    "현재 기기의 IANA 시간대. 생략 시 저장 시간대. 잘못된 값은 APP_TIMEZONE_INVALID."
)


@router.get(
    "/daily-affirmation",
    response_model=DailyAffirmationResponse,
    operation_id="getDailyAffirmation",
)
async def get_daily_affirmation(
    x_app_locale: Annotated[
        str | None, Header(max_length=64, description=_LOCALE_HEADER_DESCRIPTION)
    ] = None,
    x_app_timezone: Annotated[
        str | None, Header(description=_TIMEZONE_HEADER_DESCRIPTION)
    ] = None,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await affirmation.status(
        session, user_id, locale=x_app_locale, timezone_name=x_app_timezone
    )


@router.post(
    "/daily-affirmation/acknowledge",
    response_model=DailyAffirmationAcknowledgeResponse,
    operation_id="acknowledgeDailyAffirmation",
)
async def acknowledge_daily_affirmation(
    body: DailyAffirmationAcknowledgeRequest,
    x_app_timezone: Annotated[
        str | None, Header(description=_TIMEZONE_HEADER_DESCRIPTION)
    ] = None,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await affirmation.acknowledge(
        session, user_id, local_date=body.local_date, timezone_name=x_app_timezone
    )
