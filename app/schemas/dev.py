"""로컬 전용 개발 API 스키마."""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

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


class NoEntryDiagnostics(StrictResponse):
    created: Literal[False]
    skipped: Literal[False]
    reason: Literal["no_scheduled_entry"]
    source: Literal["none"]
    user_chars: int = Field(ge=0)
    gate: JsonValue
    gate_passed: bool
    personal_attempted: bool
    hint: str


class GeneratedDiary(StrictResponse):
    id: UUID
    source: Literal["llm", "preset", "welcome"]
    weather: Literal["sunny", "cloudy", "rainy", "windy"]
    content: str
    published_at: UtcDatetime | None


class DiaryGenerateResponse(StrictResponse):
    target_date: date
    diagnostics: SkippedDiagnostics | CreatedDiagnostics | NoEntryDiagnostics
    diary: GeneratedDiary | None


# --- 대화 모델 A/B 평가(dev 전용, /dev/chat-eval) ---
Provider = Literal["anthropic", "openai", "gemini"]


class EvalMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ModelRef(BaseModel):
    # protected_namespaces=() — 'model' 필드가 pydantic model_ 예약 네임스페이스와 충돌하는 경고 억제.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    provider: Provider
    model: str = Field(description="API 모델 ID (예: gpt-5.6-luna, gemini-3.6-flash, claude-sonnet-5)")


class ChatEvalRequest(BaseModel):
    # Swagger "Try it out" 기본 본문 — 그대로 Execute하면 바로 동작(수정 불필요).
    model_config = ConfigDict(
        extra="forbid",
        protected_namespaces=(),
        json_schema_extra={
            "example": {
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "messages": [{"role": "user", "content": "안녕 캐피 오늘 뭐했어?"}],
                "use_persona": True,
                "language": "ko",
                "max_tokens": 1024,
            }
        },
    )

    provider: Provider
    model: str = Field(description="API 모델 ID")
    messages: list[EvalMessage] = Field(
        min_length=1, max_length=50, description="user/assistant 번갈아. 마지막은 user."
    )
    use_persona: bool = Field(default=True, description="캐피 페르소나(system_prompt) 주입 여부.")
    language: str = Field(default="ko")
    max_tokens: int = Field(default=1024, ge=1, le=4096)


class ChatCompareRequest(BaseModel):
    # Swagger "Try it out" 기본 본문 — models를 비워 둬서 그대로 Execute하면 기본 5종을 한 번에 비교.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "messages": [{"role": "user", "content": "오늘 좀 힘들었어. 그냥 들어줄래?"}],
                "use_persona": True,
                "language": "ko",
                "max_tokens": 1024,
            }
        },
    )

    messages: list[EvalMessage] = Field(min_length=1, max_length=50)
    models: list[ModelRef] | None = Field(
        default=None, max_length=10,
        description="비교할 모델 목록. 생략 시 기본 셋(현행 Sonnet + 후보 4종).",
    )
    use_persona: bool = True
    language: str = "ko"
    max_tokens: int = Field(default=1024, ge=1, le=4096)


class EvalResultOut(StrictResponse):
    model_config = ConfigDict(protected_namespaces=())

    provider: str
    model: str
    text: str | None
    latency_ms: int
    input_tokens: int
    output_tokens: int
    est_cost_usd: float
    error: str | None


class ChatCompareResponse(StrictResponse):
    results: list[EvalResultOut]


# --- 실제 agent recall service 직접 진단(dev 전용) ---
class RecallMemoryRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "query": "전에 내가 힘들다고 했던 거 기억나?",
                "need": "summary",
                "limit": 5,
            }
        },
    )

    query: str = Field(min_length=1, max_length=500)
    need: Literal["summary", "exact", "quote"] = "summary"
    from_date: date | None = None
    to_date: date | None = None
    limit: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def valid_window(self) -> "RecallMemoryRequest":
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date must be on or before to_date")
        return self


class RecallDiariesRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "query": "우리 처음 만났을 때 쓴 일기",
                "need": "full",
                "limit": 5,
            }
        },
    )

    query: str | None = Field(default=None, max_length=500)
    need: Literal["count", "summary", "full", "full_card", "quote"] = "summary"
    from_date: date | None = None
    to_date: date | None = None
    focus_id: UUID | None = None
    limit: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def valid_request(self) -> "RecallDiariesRequest":
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date must be on or before to_date")
        # 인자 없는 호출(=최근 일기)을 막지 않는다. 그게 "일기 보여줘"의 정상 경로인데
        # 진단에서 재현할 수 없으면 문제를 확인할 방법이 사라진다.
        return self


class RecallEntityRef(StrictResponse):
    type: Literal["diary", "memory_fact", "memory_episode"]
    id: str = Field(min_length=1)


class DiaryRecallItem(StrictResponse):
    ref: RecallEntityRef
    id: str
    kind: Literal["welcome", "shared_day", "capi_day"]
    display_date: date
    title: str | None
    # 질의에 맞은 게 하나도 없을 때는 본문을 빼고 날짜·제목만 준다(지어내기 방지). 그래서
    # excerpt도 비어 있을 수 있다.
    excerpt: str | None
    body: str | None
    weather: str
    read: bool
    content_match: bool


class DiaryRecallResult(StrictResponse):
    # no_content_match = 질의에 맞은 일기가 없어 최근 일기를 본문 없이 돌려준 상태.
    status: Literal["ok", "no_content_match"]
    matched_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    coverage: Literal["complete", "partial"]
    has_more: bool
    items: list[DiaryRecallItem]


class MemoryRecallItem(StrictResponse):
    ref: RecallEntityRef
    id: str
    type: Literal["fact", "episode"]
    kind: str
    text: str
    observed_at: UtcDatetime | None
    similarity: float
    quote_verified: bool | None = None


class MemoryRecallResult(StrictResponse):
    status: Literal["ok"]
    matched_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    coverage: Literal["top_k"]
    has_more: bool
    items: list[MemoryRecallItem]


class MemoryRecallDiagnosticsResponse(StrictResponse):
    capability: Literal["recall_memory"]
    result: MemoryRecallResult


class DiaryRecallDiagnosticsResponse(StrictResponse):
    capability: Literal["recall_diaries"]
    result: DiaryRecallResult
