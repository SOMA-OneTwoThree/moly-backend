"""문의 — 인앱 폼 제출을 저장. 계정당 다건 허용."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import Feedback
from app.schemas.feedback import CreateFeedbackRequest
from app.services.account import _uid


async def create_feedback(
    session: AsyncSession, user_id: str, req: CreateFeedbackRequest
) -> None:
    session.add(Feedback(user_id=_uid(user_id), message=req.message, contact=req.contact))
    await session.commit()
