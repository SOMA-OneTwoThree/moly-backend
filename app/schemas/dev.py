"""로컬 전용 개발 API 스키마."""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.schemas.common import StrictResponse, UtcDatetime


class DiaryGenerateRequest(BaseModel):
    """워커 04:00 배치를 손으로 1회 돌린다(로컬 전용)."""

    model_config = ConfigDict(extra="forbid")

    target_date: date | None = Field(
        default=None,
        description="일기 대상 날짜. 생략 시 오늘(로컬 activity_date). 이 날짜의 대화가 재료가 된다.",
    )
    force: bool = Field(
        default=True,
        description="기존 일기 행을 지우고 재생성. false면 이미 있을 때 조용히 스킵된다(멱등).",
    )
    publish_now: bool = Field(
        default=True,
        description="published_at을 지금으로. false면 익일 09시라 GET /diaries에 안 보인다.",
    )


class SkippedDiagnostics(StrictResponse):
    created: Literal[False]
    skipped: Literal[True]
    reason: Literal["already_exists"]
    hint: str


class CreatedDiagnostics(StrictResponse):
    created: Literal[True]
    skipped: Literal[False]
    source: Literal["llm", "preset"]
    user_chars: int = Field(ge=0)
    gate: JsonValue
    gate_passed: bool
    personal_attempted: bool
    empty_body: bool | None
    self_check_passed: bool | None
    diary_id: UUID | None
    hint: str


class GeneratedDiary(StrictResponse):
    id: UUID
    source: Literal["llm", "preset", "welcome"]
    weather: Literal["sunny", "cloudy", "rainy", "windy"]
    content: str
    published_at: UtcDatetime | None


class DiaryGenerateResponse(StrictResponse):
    target_date: date
    diagnostics: SkippedDiagnostics | CreatedDiagnostics
    diary: GeneratedDiary | None
