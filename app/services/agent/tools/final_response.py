"""모델의 최종 텍스트와 서버 제어 sidecar를 한 번에 받는 내부 도구.

실행 도구가 아니다. 첫 홉에서는 읽기 도구 대신 이 도구를 선택하면 한 번의 호출로 턴을 끝내고,
읽기 도구를 선택한 경우 두 번째 홉에서 이 도구를 강제로 선택한다. ID는 런타임 allowlist 검증 전에는
신뢰하지 않는다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


NAME = "finish_response"
DESCRIPTION = (
    "Finish the conversational reply. Use only reference IDs returned by tools in this turn. "
    "Never mention tools, databases, searches, or server failures. Distinguish no matches from unavailable "
    "or truncated results."
)


class SelectedRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["diary", "memory_fact", "memory_episode", "routine", "item"]
    id: str = Field(min_length=1, max_length=128)


class FinalControlIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["forget", "pin"]
    target_fact_ids: list[str] = Field(default_factory=list, max_length=20)
    value: str | None = Field(default=None, max_length=500)
    future_learning: Literal["allow", "block"] = "allow"


class FinishResponseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4_000)
    response_mode: Literal["summary", "short_quote", "full_card", "reopen_reference"] = "summary"
    selected_refs: list[SelectedRef] = Field(default_factory=list, max_length=3)
    focus_ref: SelectedRef | None = None
    control_intents: list[FinalControlIntent] = Field(default_factory=list, max_length=3)


def wire_schema() -> dict:
    schema = FinishResponseArgs.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            prop.pop("title", None)
    return {
        "type": "function",
        "function": {"name": NAME, "description": DESCRIPTION, "parameters": schema},
    }
