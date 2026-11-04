"""오늘의 작은 실천 API 계약."""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from app.schemas.common import StrictResponse, UtcDatetime
from app.schemas.routine import RoutineResponse

DailyPracticeStatus = Literal["offered", "accepted", "completed"]


class DailyPractice(StrictResponse):
    id: UUID
    status: DailyPracticeStatus
    title: str
    routine_added: bool
    deadline: UtcDatetime | None = None


class DailyPracticeToday(StrictResponse):
    local_date: date
    expires_at: UtcDatetime
    locale: Literal["ko", "en", "ja"]
    can_choose: bool
    options: list[DailyPractice]
    practice: DailyPractice | None


class DailyPracticeRoutineResponse(StrictResponse):
    today: DailyPracticeToday
    routine: RoutineResponse
