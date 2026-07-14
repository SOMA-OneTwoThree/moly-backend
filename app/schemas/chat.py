"""대화 요청 스키마. 메시지 길이 상한 = 비용 통제(ERD §5.2)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PostMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    greeting_id: str | None = None  # 화면에 떠 있던 미커밋 선발화(있으면 커밋)


class GreetingResponse(BaseModel):
    """선발화 = 하루 1회. 이미 냈거나 오늘 유저가 말했으면 두 필드 모두 null(인사 없음)."""

    model_config = ConfigDict(extra="forbid")

    greeting_id: str | None = None
    content: str | None = None
