"""사용자 감정일기 API. Bearer 인증."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.mood import MoodEntryResponse, MoodListResponse, MoodPutRequest
from app.services import mood

router = APIRouter(tags=["mood"])


@router.get("/moods", response_model=MoodListResponse)
async def list_moods(
    month: str = Query(..., pattern=r"^[0-9]{4}-[0-9]{2}$"),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await mood.list_moods(session, user_id, month)


@router.put("/moods/{date}", response_model=MoodEntryResponse)
async def put_mood(
    req: MoodPutRequest,
    date: str = Path(..., pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await mood.put_mood(session, user_id, date, req)


@router.delete("/moods/{date}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mood(
    date: str = Path(..., pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await mood.delete_mood(session, user_id, date)
