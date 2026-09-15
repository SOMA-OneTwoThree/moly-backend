"""도구 registry — 런타임에 노출되는 **유일한** 도구 목록.

`runtime._resolve_registry()`가 이 모듈의 `REGISTRY`를 import한다. 여기 없는 도구는 wire 스키마에도
없고 실행도 되지 않는다 — 파일이 존재한다는 것과 켜져 있다는 것은 다르다.

| 도구 | 상태 | 사유 |
|---|---|---|
| `recall_diaries` | **등록** | 존재·개수·목록·전문을 한 번에 완결하는 일기 회상 |
| `get_routines` | **등록** | 달력 날짜 기준 루틴 상세 |
| `finish_response` | **최종 홉 내부 계약** | 응답 mode·선택 ref·focus를 typed sidecar로 확정 |
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.services.agent.tools import final_response, get_routines, recall_diaries
from app.services.agent.tools.base import BaseTool, wire_schema

# registry에 실제로 올라가는 도구. 순서가 곧 wire 스키마 순서다 —
# 프리픽스 캐시가 살려면 이 순서가 요청마다 같아야 하므로 tuple로 고정한다.
_ENABLED: tuple[BaseTool, ...] = (
    recall_diaries.TOOL,
    get_routines.TOOL,
)

# 제어 도구 — 모델이 답변 외의 의도를 전달하는 통로.
#
# 지금은 비어 있다. `forget_memory`가 유일한 제어 도구였는데, "잊어줘"를 대화로 처리하는
# 것은 의미가 없다는 제품 판단으로 제거했다(2026-08-06). 2차 호출에는 `final_response`만
# 노출된다.
_CONTROL_TOOLS: dict[str, str] = {}

_CONTROL_SCHEMAS: tuple[dict, ...] = ()


def _check_ascii(tools: Sequence[BaseTool]) -> None:
    """name/description은 ASCII 고정 영어여야 한다 — 언어별로 갈리면 프리픽스 캐시가 쪼개진다."""
    for t in tools:
        if not (t.name.isascii() and t.description.isascii()):
            raise ValueError(f"도구 name/description은 ASCII여야 한다: {t.name!r}")


class ToolRegistry:
    """이름 → 도구. wire 스키마와 입력 모델은 조립 시점에 한 번만 만든다(요청마다 재생성 X)."""

    def __init__(self, tools: Sequence[BaseTool]):
        _check_ascii(tools)
        names = [t.name for t in tools]
        if len(set(names)) != len(names):
            raise ValueError(f"도구 이름 중복: {names}")
        self._tools: dict[str, BaseTool] = {t.name: t for t in tools}
        self._schemas: tuple[dict, ...] = tuple(wire_schema(t) for t in tools)
        self._input_models: dict[str, type] = {t.name: t.input_model for t in tools}

    def wire_schemas(self) -> Sequence[dict]:
        return self._schemas

    def input_models(self) -> Mapping[str, type]:
        return dict(self._input_models)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def control_tools(self) -> Mapping[str, str]:
        return _CONTROL_TOOLS

    def control_wire_schemas(self) -> Sequence[dict]:
        return _CONTROL_SCHEMAS

    def final_response_name(self) -> str:
        return final_response.NAME

    def final_response_schema(self) -> dict:
        return final_response.wire_schema()

    def final_response_model(self) -> type:
        return final_response.FinishResponseArgs


REGISTRY = ToolRegistry(_ENABLED)
