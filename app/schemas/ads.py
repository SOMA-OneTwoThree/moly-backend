"""리워드 광고 성공 응답 스키마."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import StatusResponse, StrictResponse


class RewardAdSessionResponse(StrictResponse):
    reward_session_id: UUID
    admob_user_id: UUID
    views_used: int = Field(ge=0)
    views_limit: int = Field(ge=0)


class AdSsvResponse(StatusResponse):
    """SSV 콜백 응답 — HTTP는 항상 200(Google 재시도 정책), 처리 결과는 result로 구분."""

    result: Literal[
        "granted", "invalid_session", "session_not_found",
        "duplicate", "daily_limit", "transaction_conflict",
    ]
