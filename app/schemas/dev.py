"""로컬 전용 개발 API 스키마."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


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
