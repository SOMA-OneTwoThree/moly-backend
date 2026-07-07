"""리뷰 API. Bearer 인증."""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.services import review

router = APIRouter(tags=["review"])


@router.post("/review/prompted", status_code=status.HTTP_204_NO_CONTENT)
async def prompted(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await review.mark_prompted(session, user_id)
