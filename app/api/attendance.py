"""출석 API — 앱 진입 출석 체크와 연속 출석. 전 엔드포인트 Bearer 인증."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.economy import AttendanceCheckInResponse, AttendanceResponse
from app.schemas.routine import CalendarDate
from app.services import attendance

router = APIRouter(prefix="/attendance", tags=["economy"])


@router.get("", response_model=AttendanceResponse)
async def attendance_record(
    response: Response,
    start: Annotated[CalendarDate, Query(alias="from")],
    end: Annotated[CalendarDate, Query(alias="to")],
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    return await attendance.get_attendance(session, user_id, start, end)


@router.post("/check-in", response_model=AttendanceCheckInResponse)
async def check_in(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await attendance.check_in(session, user_id)
