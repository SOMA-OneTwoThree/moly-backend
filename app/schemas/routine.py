"""루틴 요청 스키마. 주기 = 주 N회(1~7)."""
from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field


class CreateRoutineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    frequency_per_week: int = Field(ge=1, le=7)
    reminder_enabled: bool = False
    reminder_time: time | None = None


class PatchRoutineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=50)
    frequency_per_week: int | None = Field(default=None, ge=1, le=7)
    reminder_enabled: bool | None = None
    reminder_time: time | None = None
