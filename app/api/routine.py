"""루틴 API. 전 엔드포인트 Bearer 인증."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_day import AppDay, validate_app_timezone
from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.routine import (
    CreateRoutineRequest,
    PatchRoutineRequest,
    RoutineCompleteResponse,
    RoutineListResponse,
    RoutineResponse,
    RoutineStatisticsResponse,
)
from app.services import routine
from app.services.account import _load_profile

router = APIRouter(prefix="/routines", tags=["routine"])


async def request_day(
    response: Response,
    x_app_timezone: str | None = Header(default=None),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AppDay:
    now = datetime.now(timezone.utc)
    timezone_name = validate_app_timezone(x_app_timezone)
    if timezone_name is None:
        timezone_name = (await _load_profile(session, user_id)).timezone
    day = AppDay.at(now, timezone_name)
    response.headers.update(day.headers())
    return day


@router.get("", response_model=RoutineListResponse)
async def list_routines(
    day: AppDay = Depends(request_day),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await routine.list_routines(session, user_id, day=day)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RoutineResponse)
async def create_routine(
    req: CreateRoutineRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await routine.create_routine(session, user_id, req)


@router.patch("/{routine_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_routine(
    routine_id: str,
    req: PatchRoutineRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await routine.update_routine(session, user_id, routine_id, req)


@router.delete("/{routine_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_routine(
    routine_id: str,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await routine.delete_routine(session, user_id, routine_id)


@router.post("/{routine_id}/complete", response_model=RoutineCompleteResponse)
async def complete(
    routine_id: str,
    day: AppDay = Depends(request_day),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await routine.complete(session, user_id, routine_id, day=day)


@router.delete("/{routine_id}/complete", status_code=status.HTTP_204_NO_CONTENT)
async def uncomplete(
    routine_id: str,
    day: AppDay = Depends(request_day),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await routine.uncomplete(session, user_id, routine_id, day=day)


@router.get("/{routine_id}/statistics", response_model=RoutineStatisticsResponse)
async def statistics(
    routine_id: str,
    day: AppDay = Depends(request_day),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await routine.statistics(session, user_id, routine_id, day=day)
