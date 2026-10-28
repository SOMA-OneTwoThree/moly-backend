"""탐색 트레이닝 API 계약. 공개 단계에는 정답·힌트·풀이·채점 기준이 없다."""

from __future__ import annotations

import unicodedata
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import StrictResponse, UtcDatetime

ANSWER_MAX_LENGTH = 200


def _answer_text(value: str) -> str:
    """NFC로 맞추고 앞뒤 공백을 지운 뒤 1~200 코드 포인트(DB char_length와 같은 단위)."""
    value = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(value) <= ANSWER_MAX_LENGTH:
        raise ValueError("answer must be 1-200 characters")
    return value


class TrainingCard(StrictResponse):
    id: str
    title: str
    icon_key: str
    stage_count: int = Field(ge=1)
    passed_count: int = Field(ge=0)
    completed: bool


class TrainingListResponse(StrictResponse):
    trainings: list[TrainingCard]


class TrainingLineView(StrictResponse):
    speaker: Literal["narration", "other", "me"]
    name: str | None = Field(description="Set only when another person speaks.")
    text: str | None = Field(description="On the answer slot line, the words before the blank.")
    slot: bool


class TrainingOptionView(StrictResponse):
    id: str
    text: str


class TrainingStageView(StrictResponse):
    icon_key: str | None = None
    number: int = Field(ge=1)
    answer_type: Literal["pick", "write"]
    layout: Literal["chat", "diary"]
    pick_style: Literal["choose", "erase"] | None = Field(description="Set only for pick stages.")
    review_point: bool
    reward_hay: int = Field(ge=0)
    mission: str
    lines: list[TrainingLineView]
    options: list[TrainingOptionView]
    max_length: int | None = Field(description="Code points; set only for write stages.")


class TrainingProgress(StrictResponse):
    next_stage: int | None = Field(ge=1, description="Null when the training is completed.")
    passed_count: int = Field(ge=0)
    completed: bool


class TrainingAnswerView(StrictResponse):
    stage: int = Field(ge=1)
    option_id: str | None
    text: str | None
    passed_at: UtcDatetime


class TrainingTries(StrictResponse):
    left: int = Field(ge=0)
    max: int = Field(ge=1)
    next_refill_at: UtcDatetime | None = Field(description="Set only while fewer than max.")
    full_at: UtcDatetime | None = Field(description="When all tries are back; set only while fewer than max.")


class TrainingDetail(StrictResponse):
    id: str
    title: str
    summary: str
    icon_key: str
    stages: list[TrainingStageView]
    progress: TrainingProgress
    answers: list[TrainingAnswerView]
    tries: TrainingTries


class TrainingRewardGrant(StrictResponse):
    granted: int = Field(ge=1)
    balance_after: int


class TrainingAnswerResult(StrictResponse):
    result: Literal["passed", "retry"]
    judge: Literal["answer_key", "rule", "ai", "fallback"]
    hint: str | None = Field(description="Set only when the answer did not pass.")
    explanation: str | None = Field(description="Set only when a stage with an explanation passed.")
    reward: TrainingRewardGrant | None = Field(description="Set only on the first pass of a review point.")
    answer: TrainingAnswerView | None = Field(description="The saved answer; set only when passed.")
    progress: TrainingProgress
    tries: TrainingTries


class TrainingAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: UUID = Field(description="Reuse the same id when resending the same answer.")
    option_id: str | None = Field(default=None, min_length=1, max_length=16)
    text: str | None = None

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return None if value is None else _answer_text(value)

    @model_validator(mode="after")
    def one_answer(self) -> TrainingAnswerRequest:
        if (self.option_id is None) == (self.text is None):
            raise ValueError("send exactly one of option_id or text")
        return self


class TrainingClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal["should_pass", "should_fail", "wrong_hint", "technical_error", "other"]
    answer_text: str
    verdict: Literal["passed", "retry"]
    hint: str | None = Field(default=None, max_length=300)

    @field_validator("answer_text")
    @classmethod
    def normalize_answer(cls, value: str) -> str:
        return _answer_text(value)

    @field_validator("hint")
    @classmethod
    def blank_hint_is_none(cls, value: str | None) -> str | None:
        return (value or "").strip() or None
