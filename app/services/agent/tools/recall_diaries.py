"""자연스러운 대화용 일기 회상 도구."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import memory_embeddings, recall_diaries
from app.services.agent.runtime import ToolContext
from app.services.agent.tools.base import BaseTool, InvalidArguments, ToolArgs


# ⚠️ 인자 설명은 모델이 이 도구를 쓰는 **유일한 안내**다. 한동안 설명이 하나도 없어서 모델이
# 사용자 발화를 통째로 `query`에 넣었고("어제 일기 보여줘"), 그게 하드 필터로 걸려 0건이 나왔다.
# 그러면 캐피가 "일기를 꺼내 볼 수 없다"고 답하고 다음 턴에 내용을 지어냈다(dev 실측).
#
# 일기 본문은 그날의 감정과 사건이라 "일기"라는 말 자체와는 의미가 겹치지 않는다. 실측에서
# '안녕'(0.331)이 '어제 일기'(0.233)보다 높게 나왔다 — 유사도 문턱으로는 두 종류를 못 가른다.
# 그래서 **"찾을 내용이 없으면 query를 비워라"**를 설명으로 못박는다. 비우면 최신순으로 나온다.
#
# from/to 사용은 일부러 권하지 않는다. 프롬프트에 **연도가 없어서**(날짜 표식이 "8월 6일 목요일")
# 모델이 해를 추측해야 하고, 틀리면 조용히 0건이 된다.
class RecallDiariesArgs(ToolArgs):
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Topic to look for inside diary bodies, e.g. a person, place, or event the user "
            "mentions. OMIT this field entirely when the user just asks to see a diary "
            "('show me yesterday's diary', 'read today's diary', 'what did you write'). "
            "Omitting it returns the most recent diaries, which is what those requests want. "
            "Never send an empty string and never put the user's whole sentence here."
        ),
    )
    need: Literal["count", "summary", "full", "full_card", "quote"] = Field(
        default="summary",
        description=(
            "How much of each diary to return. 'count' for existence or how many, "
            "'summary' for dates and short excerpts, 'full' when the user wants to read it."
        ),
    )
    from_: dt.date | None = Field(
        default=None,
        alias="from",
        description=(
            "Earliest diary date (YYYY-MM-DD). Only set when the user names an explicit "
            "calendar date. Do not guess a date for words like 'yesterday'. Omit query, "
            "from and to entirely instead and the most recent diaries come back."
        ),
    )
    to: dt.date | None = Field(
        default=None,
        description="Latest diary date (YYYY-MM-DD). Same rule as 'from'.",
    )
    focus_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Diary id to reopen, taken from the focus block when the user says "
            "'that one' or 'the second one'. Overrides query."
        ),
    )
    limit: int = Field(
        default=3, ge=1, le=5, description="How many diaries to return at most."
    )


class RecallDiariesOut(BaseModel):
    status: str
    matched_count: int
    returned_count: int
    coverage: str
    has_more: bool
    items: list[dict]


class RecallDiariesTool(BaseTool):
    name = "recall_diaries"
    description = (
        "Recall the user's published diaries conversationally. Returns counts, coverage, titles, "
        "dates, excerpts, and full bodies in one call. "
        "For 'show me my diary' style requests call it with no arguments except need, which "
        "returns the most recent diaries. Only set query when searching for specific content. "
        "Each item says whether it actually matched the query: when content_match is false the "
        "diary is simply a recent one, not an answer to what was asked, so do not present it as "
        "the diary the user meant."
    )
    input_model = RecallDiariesArgs
    output_model = RecallDiariesOut

    async def prepare(self, ctx: ToolContext, args: dict) -> list[float] | None:
        query = args.get("query")
        return await memory_embeddings.embed_query(str(query)) if query else None

    async def run(self, ctx, args, session):  # pragma: no cover
        raise RuntimeError("recall_diaries requires prepared execution")

    async def run_prepared(
        self,
        ctx: ToolContext,
        args: RecallDiariesArgs,
        session: AsyncSession,
        prepared: list[float] | None,
    ) -> tuple[RecallDiariesOut, bool]:
        if args.from_ and args.to and args.from_ > args.to:
            raise InvalidArguments("from_after_to")
        result = await recall_diaries.recall(
            session,
            ctx.user_id,
            query=args.query,
            need=args.need,
            from_date=args.from_,
            to_date=args.to,
            focus_id=args.focus_id,
            limit=args.limit,
            query_embedding=prepared,
        )
        return RecallDiariesOut.model_validate(result), result["coverage"] == "partial"


TOOL = RecallDiariesTool()
