"""문의 API. Bearer 인증."""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.feedback import CreateFeedbackRequest
from app.services import feedback

router = APIRouter(tags=["feedback"])


@router.post("/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def create_feedback(
    req: CreateFeedbackRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await feedback.create_feedback(session, user_id, req)
