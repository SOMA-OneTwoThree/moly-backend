"""일기 API — 목록·상세·열람표시. 인증만(열람은 등급무관 무료)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.services import diary as diary_service

router = APIRouter(tags=["diary"])


@router.get("/diaries")
async def list_diaries(
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await diary_service.list_diaries(session, user_id, limit=limit, cursor=cursor)


@router.get("/diaries/{diary_id}")
async def get_diary(
    diary_id: str,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await diary_service.get_diary(session, user_id, diary_id)


@router.post("/diaries/{diary_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    diary_id: str,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await diary_service.mark_read(session, user_id, diary_id)
