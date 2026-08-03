"""`get_routines` — 유저 루틴 목록 + 그날 수행 여부.

주기 표현은 **현행 그대로**다: `days_of_week`은 ISO 1=월…7=일이고 `frequency_per_week`는
`len(days_of_week)`다(`app/services/routine.py::_dto`와 같은 규칙). 도구를 위해 새 주기 enum을
만들면 앱 응답과 모델이 보는 값이 갈라진다.

이름은 유저 자유 입력이 섞이는 **유일한** 필드다(기본 루틴은 `name_i18n`, 편집하면 `name`).
그래서 `i18n.localized_name`으로 해석한 뒤 반드시 살균(`clip`)을 통과시킨다.
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.routine import Routine, RoutineCompletion
from app.services import i18n
from app.services.agent.runtime import ToolContext
from app.services.agent.tools.base import BaseTool, InvalidArguments, ToolArgs, clip

MAX_ROWS = 20
MAX_NAME_CHARS = 100
MAX_TOTAL_CHARS = 2_000
# 루틴은 "이번 주/지난주" 맥락에서만 쓸모가 있다 — 한 달 밖을 물으면 인자 오류다.
MAX_DATE_OFFSET_DAYS = 31


class GetRoutinesArgs(ToolArgs):
    date: dt.date | None = Field(
        default=None,
        description="Date in YYYY-MM-DD to check completion for. Defaults to today.",
    )


class RoutineItem(BaseModel):
    id: str
    name: str
    frequency_per_week: int
    days_of_week: list[int]
    completed: bool


class GetRoutinesOut(BaseModel):
    items: list[RoutineItem]


class GetRoutinesTool(BaseTool):
    name = "get_routines"
    description = (
        "List the user's own routines with their weekly schedule "
        "(days_of_week uses ISO weekdays, 1=Monday..7=Sunday) "
        "and whether each one was completed on the given date."
    )
    input_model = GetRoutinesArgs
    output_model = GetRoutinesOut

    async def run(
        self, ctx: ToolContext, args: GetRoutinesArgs, session: AsyncSession
    ) -> tuple[GetRoutinesOut, bool]:
        target = args.date or ctx.activity_date
        if abs((target - ctx.activity_date).days) > MAX_DATE_OFFSET_DAYS:
            raise InvalidArguments("date_out_of_range")

        rows = list(
            (
                await session.execute(
                    select(Routine)
                    .where(
                        Routine.user_id == ctx.user_id,  # 서버 주입
                        Routine.deleted_at.is_(None),  # soft-deleted 제외
                    )
                    # 앱 목록과 같은 순서(created_at) + id로 동률을 깬다(안정 정렬).
                    .order_by(Routine.created_at, Routine.id)
                    # 상한보다 1건 더 읽어 "더 있었다"를 truncated로 알린다.
                    .limit(MAX_ROWS + 1)
                )
            ).scalars().all()
        )
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        if not rows:
            return GetRoutinesOut(items=[]), False

        done = set(
            (
                await session.execute(
                    select(RoutineCompletion.routine_id).where(
                        RoutineCompletion.user_id == ctx.user_id,  # completion도 같은 user
                        RoutineCompletion.activity_date == target,  # 같은 date
                        RoutineCompletion.routine_id.in_([r.id for r in rows]),
                    )
                )
            ).scalars().all()
        )

        items: list[RoutineItem] = []
        budget = MAX_TOTAL_CHARS
        for r in rows:
            name, cut = clip(
                i18n.localized_name(
                    r.name_i18n, ctx.language, r.name, kind="routine", key=str(r.id)
                ),
                MAX_NAME_CHARS,
            )
            truncated = truncated or cut
            if len(name) > budget:
                # 전체 문자 예산 소진 — 남은 루틴은 통째로 뺀다(반쯤 잘린 이름보다 없는 편이 낫다).
                truncated = True
                break
            budget -= len(name)
            days = [int(d) for d in (r.days_of_week or [])]
            items.append(
                RoutineItem(
                    id=str(r.id),
                    name=name,
                    frequency_per_week=len(days),  # 현행 계약: 항상 요일 수
                    days_of_week=days,
                    completed=r.id in done,
                )
            )
        return GetRoutinesOut(items=items), truncated


TOOL = GetRoutinesTool()
