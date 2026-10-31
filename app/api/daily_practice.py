"""오늘의 작은 실천 API. 전 엔드포인트 Bearer 인증."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.daily_practice import DailyPracticeRoutineResponse, DailyPracticeToday
from app.services import daily_practice

router = APIRouter(prefix="/daily-practice", tags=["daily-practice"])

Locale = Annotated[
    str | None,
    Header(
        max_length=64,
        description=(
            "실천 문구 언어. ko, en, ja 또는 해당 언어의 지역 태그를 받으며 "
            "미설정·미지원 언어는 en으로 폴백한다."
        ),
    ),
]
Timezone = Annotated[
    str | None,
    Header(description="현재 기기의 IANA 시간대. 생략 시 저장 시간대. 잘못된 값은 APP_TIMEZONE_INVALID."),
]


@router.get("", response_model=DailyPracticeToday, operation_id="getDailyPractice")
async def get_daily_practice(
    x_app_locale: Locale = None,
    x_app_timezone: Timezone = None,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await daily_practice.status(
        session, user_id, locale=x_app_locale, timezone_name=x_app_timezone
    )


@router.post("/offer", response_model=DailyPracticeToday, operation_id="offerDailyPractice")
async def offer_daily_practice(
    x_app_locale: Locale = None,
    x_app_timezone: Timezone = None,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await daily_practice.offer(
        session, user_id, locale=x_app_locale, timezone_name=x_app_timezone
    )


@router.post("/{id}/accept", response_model=DailyPracticeToday, operation_id="acceptDailyPractice")
async def accept_daily_practice(
    id: str,
    x_app_locale: Locale = None,
    x_app_timezone: Timezone = None,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await daily_practice.accept(
        session, user_id, id, locale=x_app_locale, timezone_name=x_app_timezone
    )


@router.post("/{id}/complete", response_model=DailyPracticeToday, operation_id="completeDailyPractice")
async def complete_daily_practice(
    id: str,
    x_app_locale: Locale = None,
    x_app_timezone: Timezone = None,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await daily_practice.complete(
        session, user_id, id, locale=x_app_locale, timezone_name=x_app_timezone
    )


@router.post("/{id}/uncomplete", response_model=DailyPracticeToday, operation_id="uncompleteDailyPractice")
async def uncomplete_daily_practice(
    id: str,
    x_app_locale: Locale = None,
    x_app_timezone: Timezone = None,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await daily_practice.uncomplete(
        session, user_id, id, locale=x_app_locale, timezone_name=x_app_timezone
    )


@router.post("/{id}/routine", response_model=DailyPracticeRoutineResponse, operation_id="addDailyPracticeRoutine")
async def add_daily_practice_routine(
    id: str,
    x_app_locale: Locale = None,
    x_app_timezone: Timezone = None,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await daily_practice.add_routine(
        session, user_id, id, locale=x_app_locale, timezone_name=x_app_timezone
    )
