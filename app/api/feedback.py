"""문의 API. Bearer 인증."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.feedback import CreateFeedbackRequest
from app.services import feedback, slack_notify

router = APIRouter(tags=["feedback"])


@router.post("/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def create_feedback(
    req: CreateFeedbackRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await feedback.create_feedback(session, user_id, req)
    # 저장 성공 후 슬랙 알림 — 백그라운드(응답 204를 지연 안 시킴) + best-effort(URL 없거나 실패해도 무영향).
    background_tasks.add_task(
        slack_notify.send_summary,
        slack_notify.feedback_text(user_id, req.message, req.contact),
    )
