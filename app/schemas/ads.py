"""리워드 광고 성공 응답 스키마."""
from __future__ import annotations

from uuid import UUID

from pydantic import Field

from app.schemas.common import StrictResponse


class RewardAdSessionResponse(StrictResponse):
    reward_session_id: UUID
    admob_user_id: UUID
    views_used: int = Field(ge=0)
    views_limit: int = Field(ge=0)
