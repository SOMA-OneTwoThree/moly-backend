"""대화 요청·응답 스키마. 메시지 길이 상한 = 비용 통제(ERD §5.2)."""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import StrictResponse, UtcDatetime


class DailyFortuneContextRef(BaseModel):
    """운세 결과 화면이 현재 공개 결과를 한 번만 채팅에 붙이는 명시적 참조."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["daily_fortune"]
    local_date: date
    locale: Literal["ko"]


class PostMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    greeting_id: str | None = None  # 화면에 떠 있던 미커밋 선발화(있으면 커밋)
    context_ref: DailyFortuneContextRef | None = None


class ChatStateResponse(StrictResponse):
    activity_date: date
    plan: Literal["free", "trial", "monthly", "yearly"]
    tokens_used: int = Field(ge=0)
    daily_token_limit: int | None = Field(default=None, ge=0)
    tokens_remaining: int | None = Field(default=None, ge=0)
    warning_threshold: int = Field(ge=0, strict=True)
    personal_diary_eligible: bool
    limit_reached: bool


DiaryReferenceKind = Literal["welcome", "shared_day", "capi_day"]


class DiaryReferenceCard(StrictResponse):
    """클라이언트가 대화 안에서 펼치는 일기 원문 카드.

    모델이 만든 요약이 아니라 Phase B가 소유권·공개 상태를 재검증한 DB 원문만 담는다.
    카드 전달 자체는 읽음 처리가 아니며, 실제로 펼칠 때 기존 ``/diaries/{id}/read``를
    호출한다.
    """

    id: UUID
    kind: DiaryReferenceKind
    author: Literal["capi"] = "capi"
    display_date: date
    title: str | None
    body: str
    published_at: UtcDatetime
    read: bool


class ChatReference(StrictResponse):
    """버전이 고정된 채팅 부가자료.

    target이 삭제·비공개·억제되면 reference 행 자체는 이력 위치를 보존하되 ``unavailable``로
    내리고 본문을 보내지 않는다. 두 상태의 조합을 validator로 고정해 조용한 개인정보 누출을
    막는다.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    reference_id: UUID
    schema_version: Literal["diary-reference-v1"] = Field(
        default="diary-reference-v1", alias="schema", serialization_alias="schema"
    )
    type: Literal["diary"] = "diary"
    state: Literal["available", "unavailable"]
    mode: Literal["full_card", "reopen_reference"]
    diary: DiaryReferenceCard | None

    @model_validator(mode="after")
    def state_matches_payload(self) -> "ChatReference":
        if self.state == "available" and self.diary is None:
            raise ValueError("available reference requires a diary card")
        if self.state == "unavailable" and self.diary is not None:
            raise ValueError("unavailable reference cannot contain a diary card")
        return self


class ChatMessage(StrictResponse):
    id: str = Field(pattern=r"^\d+$")
    sender: Literal["user", "moly"]
    content: str
    created_at: UtcDatetime
    # Legacy history rows and cached POST responses omit this field. A default keeps those payloads
    # readable while new clients can opt in with X-Moly-Capabilities: diary-reference-v1.
    references: list[ChatReference] = Field(default_factory=list, max_length=3)


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
    # Optional/additive on the wire: old idempotency JSONB without the key remains valid.
    references: list[ChatReference] = Field(default_factory=list, max_length=3)


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
