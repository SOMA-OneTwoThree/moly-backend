"""도구 registry — 런타임에 노출되는 **유일한** 도구 목록.

`runtime._resolve_registry()`가 이 모듈의 `REGISTRY`를 import한다. 여기 없는 도구는 wire 스키마에도
없고 실행도 되지 않는다 — 파일이 존재한다는 것과 켜져 있다는 것은 다르다.

| 도구 | 상태 | 사유 |
|---|---|---|
| `get_diary` | **등록** | |
| `get_routines` | **등록** | |
| `search_diaries` | 미등록 | `diaries`에 검색 색인 없음(title 컬럼조차 없음). 한국어 FTS 방식(tsvector+GIN vs pgvector)은 측정 후 택1 — 그전까지 날짜 조회만 구현 |
| `search_memory` | 미등록 | W8의 normalized repository + forget hard filter가 전제. 그전에 켜면 유저가 잊어달라고 한 기억이 되살아난다 |

미등록 도구도 `_DISABLED`에 담아 둔다 — 존재를 잊지 않게 하고, "스키마에 새지 않는다"를
테스트가 이 목록으로 직접 확인할 수 있다.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.services.agent.tools import get_diary, get_routines, search_diaries, search_memory
from app.services.agent.tools.base import BaseTool, wire_schema

# registry에 실제로 올라가는 도구. 순서가 곧 wire 스키마 순서다 —
# 프리픽스 캐시가 살려면 이 순서가 요청마다 같아야 하므로 tuple로 고정한다.
_ENABLED: tuple[BaseTool, ...] = (get_diary.TOOL, get_routines.TOOL)

# 구현은 있으나 켜지 않는 도구(위 표의 사유). 스키마에도 노출하지 않는다.
_DISABLED: tuple[BaseTool, ...] = (search_diaries.TOOL, search_memory.TOOL)


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
        return self._input_models

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)


REGISTRY = ToolRegistry(_ENABLED)
