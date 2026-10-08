"""루틴 요청 스키마. 스케줄 = 요일별(days_of_week)만 지원."""
from __future__ import annotations

import re
from datetime import date, time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.common import StrictResponse

DayOfWeek = Literal[1, 2, 3, 4, 5, 6, 7]
ReminderTime = Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]
RoutineIcon = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
RoutineColor = Literal["pink", "peach", "yellow", "green", "blue", "mint", "lavender"]
TemplateId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]


def _calendar_date(value):
    if isinstance(value, str) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 해요.")
    return value


CalendarDate = Annotated[date, BeforeValidator(_calendar_date)]


def _valid_days(days: list[int]) -> list[int]:
    if not days or len(set(days)) != len(days) or any(d < 1 or d > 7 for d in days):
        raise ValueError("days_of_week는 1~7(월=1) 중복 없이 1개 이상이어야 해요.")
    return sorted(set(days))


class CreateRoutineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    days_of_week: list[int]  # 지정 요일(ISO 1=월…7=일)
    reminder_enabled: bool = False
    reminder_time: time | None = None
    icon: RoutineIcon = "seedling"
    color: RoutineColor = "peach"

    @model_validator(mode="after")
    def _check(self):
        self.days_of_week = _valid_days(self.days_of_week)
        return self


class PatchRoutineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=50)
    days_of_week: list[int] | None = None  # 필드 생략=변경 없음, 빈 배열은 422
    reminder_enabled: bool | None = None
    reminder_time: time | None = None
    icon: RoutineIcon | None = None
    color: RoutineColor | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.days_of_week is not None:
            self.days_of_week = _valid_days(self.days_of_week)
        return self


class RoutineResponse(StrictResponse):
    id: UUID
    name: str = Field(min_length=1, max_length=50)
    icon: RoutineIcon
    color: RoutineColor
    template_id: str | None
    frequency_per_week: int = Field(ge=1, le=7)
    days_of_week: list[DayOfWeek]
    reminder_enabled: bool
    reminder_time: ReminderTime | None
    completed_today: bool
    skipped_today: bool


class RoutineListResponse(StrictResponse):
    data: list[RoutineResponse]


class RoutineCompleteResponse(StrictResponse):
    completed_today: Literal[True]
    completed_count_today: int = Field(ge=0)


class WeekdayCompletion(StrictResponse):
    day_1: bool = Field(alias="1")
    day_2: bool = Field(alias="2")
    day_3: bool = Field(alias="3")
    day_4: bool = Field(alias="4")
    day_5: bool = Field(alias="5")
    day_6: bool = Field(alias="6")
    day_7: bool = Field(alias="7")


class ThisWeekStatistics(StrictResponse):
    completed_count: int = Field(ge=0)
    by_weekday: WeekdayCompletion


class RoutineHistoryItem(StrictResponse):
    id: UUID
    name: str = Field(min_length=1, max_length=50)
    icon: RoutineIcon
    color: RoutineColor
    days_of_week: list[DayOfWeek]
    reminder_enabled: bool
    reminder_time: ReminderTime | None
    completed: bool
    skipped: bool
    streak: int = Field(ge=0)
    this_week: ThisWeekStatistics


class RoutineHistoryResponse(StrictResponse):
    date: date
    data: list[RoutineHistoryItem]


class RoutineStatisticsResponse(StrictResponse):
    streak: int = Field(ge=0)
    completed_today: bool
    target_count: int = Field(ge=1, le=7)
    days_of_week: list[DayOfWeek]
    this_week: ThisWeekStatistics
    last_30_days: list[date]
    completion_rate: float = Field(ge=0, le=1)


class RoutineStatsSummary(StrictResponse):
    current_streak: int = Field(ge=0)
    best_streak: int = Field(ge=0)
    perfect_days: int = Field(ge=0)
    total_completed: int = Field(ge=0)
    completed_this_month: int = Field(ge=0)
    overall_rate: float | None = Field(ge=0, le=1)
    monthly_rate: float | None = Field(ge=0, le=1)


class RoutineStatsDay(StrictResponse):
    date: date
    scheduled: int = Field(ge=0)
    completed: int = Field(ge=0)


class RoutineStatsItem(StrictResponse):
    id: UUID
    name: str = Field(min_length=1, max_length=50)
    icon: RoutineIcon
    color: RoutineColor
    scheduled_dates: list[date]
    completed_dates: list[date]
    skipped_dates: list[date]
    current_streak: int = Field(ge=0)
    best_streak: int = Field(ge=0)
    total_completed: int = Field(ge=0)
    completed_this_month: int = Field(ge=0)
    overall_rate: float | None = Field(ge=0, le=1)


class RoutineStatsResponse(StrictResponse):
    today: date
    summary: RoutineStatsSummary
    days: list[RoutineStatsDay]
    routines: list[RoutineStatsItem]


class RoutineTemplate(StrictResponse):
    id: TemplateId
    name: str = Field(min_length=1, max_length=50)
    icon: RoutineIcon
    color: RoutineColor
    days_of_week: list[DayOfWeek]
    recommended: bool
    added: bool


class RoutineTemplateCategory(StrictResponse):
    id: TemplateId
    name: str = Field(min_length=1)
    templates: list[RoutineTemplate]


class RoutineTemplatesResponse(StrictResponse):
    selection_completed: bool
    categories: list[RoutineTemplateCategory]


class RoutineTemplateSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_ids: list[TemplateId] = Field(max_length=30)

    @field_validator("template_ids")
    @classmethod
    def _unique(cls, ids: list[str]) -> list[str]:
        if len(set(ids)) != len(ids):
            raise ValueError("template_ids는 중복 없이 보내야 해요.")
        return ids
