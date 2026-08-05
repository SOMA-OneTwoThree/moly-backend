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
    # 생략 가능하다. **그때는 가장 최근 일기**를 준다.
    #
    # ⚠️ 예전엔 오늘로 처리했다. 그런데 "저번에 써준 일기 읽어줘" 같은 요청엔 날짜가 없고,
    #    오늘은 대개 일기가 없어서 null이 돌아왔다. 그러면 캐피가 "일기가 안 보인다"고 답한다.
    #    모델이 날짜를 찍어 맞혀야 해서 같은 질문에도 됐다 안 됐다 했다(감사 실측: 3번 중 1번).
    date: dt.date | None = Field(
        default=None,
        description=(
            "Diary date in YYYY-MM-DD. **Omit this to get the most recent entry** — "
            "use it that way when the user refers to a diary without naming a date."
        ),
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
        "Read a diary entry that you (the capybara) wrote about this user. "
        "Use this whenever the user mentions their diary in any way — asking what you "
        "wrote, asking you to read it aloud, or referring to it indirectly. "
        "Omit `date` to get the most recent entry; pass `date` only when the user names "
        "a specific day. Returns null only when no entry exists at all."
    )
    input_model = GetDiaryArgs
    output_model = GetDiaryOut

    async def run(
        self, ctx: ToolContext, args: GetDiaryArgs, session: AsyncSession
    ) -> tuple[GetDiaryOut, bool]:
        if args.date is not None and abs(
            (args.date - ctx.activity_date).days
        ) > MAX_DATE_OFFSET_DAYS:
            raise InvalidArguments("date_out_of_range")

        now = dt.datetime.now(dt.timezone.utc)
        where = [
            Diary.user_id == ctx.user_id,  # 서버 주입 — 모델 인자에 없다
            Diary.published_at.is_not(None),
            Diary.published_at <= now,
        ]
        if args.date is not None:
            where.append(Diary.diary_date == args.date)
        row = (
            await session.execute(
                select(Diary)
                .where(*where)
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
