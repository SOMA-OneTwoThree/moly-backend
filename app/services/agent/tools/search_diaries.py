"""`search_diaries` — 발행된 일기의 날짜 범위·내용 검색.

한국어 형태소 사전에 의존하지 않는 pg_trgm GIN 인덱스로 부분문자열을 찾는다.
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.diary import Diary
from app.services.agent.runtime import ToolContext
from app.services.agent.tools.base import BaseTool, InvalidArguments, ToolArgs, clip

MAX_ROWS = 5
MAX_EXCERPT_CHARS = 400
MAX_TOTAL_CHARS = 2_000
# 조회폭(inclusive). 90일보다 넓은 창은 인자 오류다 — 넓힐수록 DB p95와 토큰이 같이 는다.
WINDOW_DAYS = 90


class SearchDiariesArgs(ToolArgs):
    query: str | None = Field(default=None, min_length=1, max_length=200)
    # `from`은 파이썬 예약어라 필드명은 `from_`, wire는 alias `from`이다(ToolArgs가 populate_by_name).
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None


class DiaryHit(BaseModel):
    diary_date: dt.date
    excerpt: str
    weather: str


class SearchDiariesOut(BaseModel):
    items: list[DiaryHit]


class SearchDiariesTool(BaseTool):
    name = "search_diaries"
    description = (
        "Search the user's own published diary entries within a date range "
        "(at most 90 days) and return short excerpts. "
        "Call this only when the user asks about their diaries or brings them up."
    )
    input_model = SearchDiariesArgs
    output_model = SearchDiariesOut

    async def run(
        self, ctx: ToolContext, args: SearchDiariesArgs, session: AsyncSession
    ) -> tuple[SearchDiariesOut, bool]:
        to = args.to or ctx.activity_date
        frm = args.from_ or (to - dt.timedelta(days=WINDOW_DAYS - 1))
        if frm > to:
            raise InvalidArguments("from_after_to")
        if (to - frm).days + 1 > WINDOW_DAYS:
            raise InvalidArguments("window_too_wide")
        now = dt.datetime.now(dt.timezone.utc)
        filters = [
            Diary.user_id == ctx.user_id,
            Diary.published_at.is_not(None),
            Diary.published_at <= now,
            Diary.diary_date >= frm,
            Diary.diary_date <= to,
        ]
        if args.query:
            filters.append(Diary.content.ilike(f"%{args.query}%"))
        rows = list(
            (
                await session.execute(
                    select(Diary)
                    .where(*filters)
                    .order_by(Diary.diary_date.desc(), Diary.id)
                    .limit(MAX_ROWS + 1)
                )
            ).scalars().all()
        )
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]

        items: list[DiaryHit] = []
        budget = MAX_TOTAL_CHARS
        for d in rows:
            # 예산 소진만 중단 사유다. 빈 excerpt를 중단 신호로 쓰면 본문이 빈 일기 하나 때문에
            # 뒤 일기가 통째로 사라지고 truncated까지 잘못 표시된다(예산은 아직 남았는데).
            if budget <= 0:
                truncated = True
                break
            excerpt, cut = clip(d.content, min(MAX_EXCERPT_CHARS, budget))
            truncated = truncated or cut
            budget -= len(excerpt)
            weather, _ = clip(d.weather, 16)
            items.append(
                DiaryHit(diary_date=d.diary_date, excerpt=excerpt, weather=weather)
            )
        return SearchDiariesOut(items=items), truncated


TOOL = SearchDiariesTool()
