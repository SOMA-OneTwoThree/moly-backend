"""`get_diary` — 유저 본인의 **발행된** 일기 1건을 날짜로 읽는다.

정확히 0건 또는 1건이다(`diaries`는 `UNIQUE(user_id, diary_date)`). 0건은 실패가 아니라
`status='ok', data={"diary": null}`이다 — `not_found`를 transport 실패로 만들면 모델이
"서버가 고장났다"로 읽고 사과부터 한다.

노출 조건은 일기 API(`app/services/diary.py`)와 **같다**: `published_at IS NOT NULL AND
published_at <= now`. 배치가 미리 만들어둔 다음날 일기가 도구로 새면 앱보다 먼저 스포일러가 된다.
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.diary import Diary
from app.services.agent.runtime import ToolContext
from app.services.agent.tools.base import BaseTool, InvalidArguments, ToolArgs, clip

# 본문 상한. 개별 안전장치이고, 실제 비용 상한은 턴 합계 예산(runtime.apply_result_budget)이다.
MAX_CONTENT_CHARS = 4_000
# 날씨는 서버가 정한 enum(sunny|cloudy|rainy|windy)이라 짧게 자르는 것으로 충분하다.
MAX_WEATHER_CHARS = 16
# `ctx.activity_date` 기준 허용 오프셋. 10년치 — 이 밖은 오타이거나 탐색 시도다.
MAX_DATE_OFFSET_DAYS = 3_660


class GetDiaryArgs(ToolArgs):
    # 생략 가능하고 그때는 `ctx.activity_date`(오늘)를 쓴다. 모델이 오늘 날짜를 모를 수 있어
    # 필수로 두면 "오늘 일기" 요청이 그냥 실패한다.
    date: dt.date | None = Field(
        default=None, description="Diary date in YYYY-MM-DD. Defaults to today."
    )


class DiaryEntry(BaseModel):
    diary_date: dt.date
    content: str
    weather: str


class GetDiaryOut(BaseModel):
    diary: DiaryEntry | None


class GetDiaryTool(BaseTool):
    name = "get_diary"
    description = (
        "Read the user's own diary entry for a single date. "
        "Returns null when there is no published entry for that date."
    )
    input_model = GetDiaryArgs
    output_model = GetDiaryOut

    async def run(
        self, ctx: ToolContext, args: GetDiaryArgs, session: AsyncSession
    ) -> tuple[GetDiaryOut, bool]:
        target = args.date or ctx.activity_date
        if abs((target - ctx.activity_date).days) > MAX_DATE_OFFSET_DAYS:
            raise InvalidArguments("date_out_of_range")

        now = dt.datetime.now(dt.timezone.utc)
        row = (
            await session.execute(
                select(Diary)
                .where(
                    Diary.user_id == ctx.user_id,  # 서버 주입 — 모델 인자에 없다
                    Diary.diary_date == target,
                    Diary.published_at.is_not(None),
                    Diary.published_at <= now,
                )
                # UNIQUE(user_id, diary_date)라 1건이지만 정렬·limit을 명시해 둔다(안정 정렬 규칙).
                .order_by(Diary.diary_date.desc(), Diary.id)
                .limit(1)
            )
        ).scalars().first()
        if row is None:
            return GetDiaryOut(diary=None), False

        content, truncated = clip(row.content, MAX_CONTENT_CHARS)
        weather, _ = clip(row.weather, MAX_WEATHER_CHARS)
        return (
            GetDiaryOut(
                diary=DiaryEntry(diary_date=row.diary_date, content=content, weather=weather)
            ),
            truncated,
        )


TOOL = GetDiaryTool()
