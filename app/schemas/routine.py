"""루틴 요청 스키마. 스케줄 = 요일별(days_of_week) 또는 주 N회(frequency_per_week)."""
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
