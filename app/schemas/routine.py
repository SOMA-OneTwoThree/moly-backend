"""루틴 요청 스키마. 스케줄 = 요일별(days_of_week)만 지원."""
from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    @model_validator(mode="after")
    def _check(self):
        if self.days_of_week is not None:
            self.days_of_week = _valid_days(self.days_of_week)
        return self
