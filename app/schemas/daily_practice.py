"""오늘의 작은 실천 API 계약."""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import StrictResponse, UtcDatetime
from app.schemas.routine import RoutineResponse

DailyPracticeCategory = Literal[
    "rest", "body", "calm", "mind", "self_kindness", "connect", "order"
]
DailyPracticeStatus = Literal["offered", "accepted", "completed"]


class DailyPracticeOfferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    focus: list[DailyPracticeCategory] = Field(default_factory=list, max_length=3)


class DailyPractice(StrictResponse):
    id: UUID
    status: DailyPracticeStatus
    category: DailyPracticeCategory
    title: str
    speech: str
    icon: str
    routine_added: bool


class DailyPracticeToday(StrictResponse):
    local_date: date
    expires_at: UtcDatetime
    locale: Literal["ko", "en", "ja"]
    practice: DailyPractice | None
    offers_remaining: int


class DailyPracticeRoutineResponse(StrictResponse):
    today: DailyPracticeToday
    routine: RoutineResponse
