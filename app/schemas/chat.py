"""대화 요청·응답 스키마. 메시지 길이 상한 = 비용 통제(ERD §5.2)."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import StrictResponse, UtcDatetime


class PostMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    greeting_id: str | None = None  # 화면에 떠 있던 미커밋 선발화(있으면 커밋)


class ChatStateResponse(StrictResponse):
    activity_date: date
    plan: Literal["free", "trial", "monthly", "yearly"]
    tokens_used: int = Field(ge=0)
    daily_token_limit: int | None = Field(default=None, ge=0)
    tokens_remaining: int | None = Field(default=None, ge=0)
    warning_threshold: int = Field(ge=0, strict=True)
    personal_diary_eligible: bool
    limit_reached: bool


class ChatMessage(StrictResponse):
    id: str = Field(pattern=r"^\d+$")
    sender: Literal["user", "moly"]
    content: str
    created_at: UtcDatetime


class MessagesResponse(StrictResponse):
    data: list[ChatMessage]
    older_cursor: str | None = Field(default=None, pattern=r"^\d+$")
    newer_cursor: str | None = Field(default=None, pattern=r"^\d+$")


class CommittedGreeting(StrictResponse):
    message_id: str = Field(pattern=r"^\d+$")
    content: str
    created_at: UtcDatetime


class CreatedMessage(StrictResponse):
    message_id: str = Field(pattern=r"^\d+$")
    created_at: UtcDatetime


class ReplyMessage(CreatedMessage):
    content: str


class PostMessageResponse(StrictResponse):
    greeting: CommittedGreeting | None
    user_message: CreatedMessage
    reply: ReplyMessage
    tokens_used: int = Field(ge=0)
    tokens_remaining: int = Field(ge=0)
    review_prompt: bool


class GreetingResponse(StrictResponse):
    """선발화 = 하루 1회. 이미 냈거나 오늘 유저가 말했으면 두 필드 모두 null(인사 없음)."""

    greeting_id: str | None = None
    content: str | None = None
