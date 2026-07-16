"""루틴 요청 스키마. 스케줄 = 요일별(days_of_week) 또는 주 N회(frequency_per_week)."""
from __future__ import annotations

from datetime import date, time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.common import StrictResponse

DayOfWeek = Literal[1, 2, 3, 4, 5, 6, 7]
ReminderTime = Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]


def _valid_days(days: list[int]) -> list[int]:
    if not days or len(set(days)) != len(days) or any(d < 1 or d > 7 for d in days):
        raise ValueError("days_of_week는 1~7(월=1) 중복 없이 1개 이상이어야 해요.")
    return sorted(set(days))


class CreateRoutineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    frequency_per_week: int | None = Field(default=None, ge=1, le=7)
    days_of_week: list[int] | None = None  # 지정 시 요일별 모드(frequency는 요일 수로 파생)
    reminder_enabled: bool = False
    reminder_time: time | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.days_of_week is not None:
            self.days_of_week = _valid_days(self.days_of_week)
        elif self.frequency_per_week is None:
            raise ValueError("frequency_per_week 또는 days_of_week 중 하나는 필요해요.")
        return self


class PatchRoutineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=50)
    frequency_per_week: int | None = Field(default=None, ge=1, le=7)
    # [1,3,5]=요일별 전환 · []=주 N회 전환(frequency 동반) · 필드 생략=변경 없음
    days_of_week: list[int] | None = None
    reminder_enabled: bool | None = None
    reminder_time: time | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.days_of_week:  # 비어있지 않은 리스트만 값 검증(빈 배열=모드 전환 신호)
            self.days_of_week = _valid_days(self.days_of_week)
        return self


class RoutineResponse(StrictResponse):
    id: UUID
    name: str = Field(min_length=1, max_length=50)
    frequency_per_week: int = Field(ge=1, le=7)
    days_of_week: list[DayOfWeek] | None
    reminder_enabled: bool
    reminder_time: ReminderTime | None
    completed_today: bool


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


class RoutineStatisticsResponse(StrictResponse):
    streak: int = Field(ge=0)
    completed_today: bool
    target_count: int = Field(ge=1, le=7)
    days_of_week: list[DayOfWeek] | None
    this_week: ThisWeekStatistics
    last_30_days: list[date]
    completion_rate: float = Field(ge=0, le=1)
