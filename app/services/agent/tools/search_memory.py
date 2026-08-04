"""`search_memory` — 정규화 기억의 pgvector 의미 검색.

후보 필터는 repository가 소유한다. active 상태와 forget marker를 SQL에서 먼저 적용하므로
프로필 재생성이 지연돼도 잊은 사실이 검색 결과로 되살아나지 않는다.
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import memory_embeddings, memory_repo
from app.services.agent.runtime import ToolContext
from app.services.agent.tools.base import BaseTool, InvalidArguments, ToolArgs, UtcDatetime, clip

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
        frm = args.time_hint.from_ if args.time_hint else None
        to = args.time_hint.to if args.time_hint else None
        if frm and to and frm > to:
            raise InvalidArguments("from_after_to")
        if frm and to and (to - frm).days > MAX_DATE_OFFSET_DAYS:
            raise InvalidArguments("window_too_wide")
        if frm and abs((frm - ctx.activity_date).days) > MAX_DATE_OFFSET_DAYS:
            raise InvalidArguments("date_out_of_range")
        if to and abs((to - ctx.activity_date).days) > MAX_DATE_OFFSET_DAYS:
            raise InvalidArguments("date_out_of_range")

        embedding = await memory_embeddings.embed_query(args.query)
        rows = await memory_repo.search_memory(
            session,
            ctx.user_id,
            embedding=embedding,
            from_date=frm,
            to_date=to,
            limit=MAX_ROWS + 1,
            min_similarity=settings.memory_search_min_similarity,
        )
        truncated = len(rows) > MAX_ROWS
        budget = MAX_TOTAL_CHARS
        items: list[MemoryItem] = []
        for row in rows[:MAX_ROWS]:
            if budget <= 0:
                truncated = True
                break
            text, cut = clip(row.text, min(MAX_ITEM_CHARS, budget))
            budget -= len(text)
            truncated = truncated or cut
            items.append(
                MemoryItem(
                    id=str(row.id), kind=row.kind, text=text, observed_at=row.observed_at
                )
            )
        return SearchMemoryOut(items=items), truncated


TOOL = SearchMemoryTool()
