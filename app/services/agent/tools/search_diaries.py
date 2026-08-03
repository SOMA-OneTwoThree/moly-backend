"""`search_diaries` — **registry 비활성**(구현은 날짜 기반만).

`diaries`에는 검색 색인이 없다. title 컬럼조차 없어서 지금 `query`로 매칭하려면 전 행 `ILIKE`
스캔밖에 없고, 한국어 FTS를 어떤 방식(tsvector+GIN vs pgvector)으로 넣을지는 **측정 후 택1**이다.
그때까지는 날짜 기반 조회만 구현해 두고 registry에 올리지 않는다(명세 W6).

`query` 인자는 스키마에 남겨두되 매칭에 쓰지 않는다 — 색인이 생기면 이 파일의 `run()` 안쪽만
바뀌고 인자·반환 계약은 그대로여서, 켜는 시점에 모델이 보는 표면이 흔들리지 않는다.
"""
from __future__ import annotations

import datetime as dt
import logging

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.diary import Diary
from app.services.agent.runtime import ToolContext
from app.services.agent.tools.base import BaseTool, InvalidArguments, ToolArgs, clip

_log = logging.getLogger("moly-backend")

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
        "(at most 90 days) and return short excerpts."
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
        if args.query:
            # 색인이 없어 매칭에 쓰지 않는다. 켜기 전이라 경보가 아니라 관측용 로그다.
            _log.info("search_diaries: query 인자 무시(색인 미도입) len=%d", len(args.query))

        now = dt.datetime.now(dt.timezone.utc)
        rows = list(
            (
                await session.execute(
                    select(Diary)
                    .where(
                        Diary.user_id == ctx.user_id,  # 서버 주입
                        Diary.published_at.is_not(None),
                        Diary.published_at <= now,
                        Diary.diary_date >= frm,
                        Diary.diary_date <= to,
                    )
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
