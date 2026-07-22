"""문의 — 인앱 폼 제출을 저장. 계정당 다건 허용."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import Feedback
from app.models.profile import Profile
from app.schemas.feedback import CreateFeedbackRequest
from app.services.account import _uid


async def create_feedback(
    session: AsyncSession, user_id: str, req: CreateFeedbackRequest
) -> str | None:
    """피드백 저장 후 유저 닉네임을 반환한다(슬랙 알림 표시용). 프로필 없으면 None."""
    uid = _uid(user_id)
    session.add(Feedback(user_id=uid, message=req.message, contact=req.contact))
    await session.commit()
    profile = await session.get(Profile, uid)
    return profile.nickname if profile is not None else None
