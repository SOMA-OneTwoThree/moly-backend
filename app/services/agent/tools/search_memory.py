"""`search_memory` — **registry 미등록·schema 미노출**. 인터페이스와 W8 연동 지점만 둔다.

지금 켤 수 없는 이유는 색인이 아니라 **정합성**이다. 명세가 요구하는 후보 조건은
`memory_facts.user_id=ctx.user_id AND status='active'`이면서 같은 user의 `scope='all'`,
matching predicate, fact_id, `(normalized_hash, normalization_version)` marker가 **하나도 없어야**
한다는 것이고, insight는 그 조건을 만족하는 source만으로 이뤄져야 한다. 이 hard filter는
W8의 normalized repository가 소유한다 — 그게 없는 상태로 켜면 **유저가 잊어달라고 한 기억을
도구가 되살린다**.

그래서 mem0 façade의 `load_for_context`를 query search처럼 재사용하지 **않는다**. 그건 턴 시작
시점의 컨텍스트 로드용이고 forget marker 판정을 통과한 결과가 아니다. 재사용하면 위 사고가
"검색"이라는 이름으로 그대로 난다.

**W8이 끝나면 할 일은 두 가지뿐**이다.
 1. 아래 `run()`에서 W8 repository의 hard-filtered 조회를 호출한다(fact/insight 후보 → rerank → top 5).
 2. `registry.py`의 `_ENABLED`에 이 모듈의 `TOOL`을 추가한다.
rerank 가중치(relevance·importance·recency·confidence)는 shadow 평가 데이터로 확정하기 전에는
등록하지 않는다(**측정 필요**).
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent.runtime import ToolContext
from app.services.agent.tools.base import BaseTool, ToolArgs, UtcDatetime

MAX_ROWS = 5
MAX_ITEM_CHARS = 300
MAX_TOTAL_CHARS = 1_500
MAX_DATE_OFFSET_DAYS = 3_660


class TimeHint(ToolArgs):
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None


class SearchMemoryArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=200)
    time_hint: TimeHint | None = None


class MemoryItem(BaseModel):
    id: str
    kind: str  # fact | insight
    text: str
    observed_at: UtcDatetime | None = None


class SearchMemoryOut(BaseModel):
    items: list[MemoryItem]


class SearchMemoryTool(BaseTool):
    name = "search_memory"
    description = (
        "Search what the assistant remembers about the user, "
        "optionally restricted to a time range."
    )
    input_model = SearchMemoryArgs
    output_model = SearchMemoryOut

    async def run(
        self, ctx: ToolContext, args: SearchMemoryArgs, session: AsyncSession
    ) -> tuple[SearchMemoryOut, bool]:
        # W8 연동 지점. registry에 없으므로 정상 경로에서는 호출되지 않는다.
        raise NotImplementedError(
            "search_memory는 W8(normalized repository + forget hard filter) 이후에만 켠다"
        )


TOOL = SearchMemoryTool()
