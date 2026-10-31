"""오늘의 작은 실천 풀 보충 — 선택이 부족할 때 카테고리당 하루 한 번 돈다.

사용자 데이터 없이 카테고리와 시간대만 받는다(`user_id` 없음). 실패는 조용히 끝낸다. 재시도나 dead 경보
없이 다음 날 다시 요청될 때 이어 가고, 사용자 요청은 원래 이 잡을 기다리지 않는다.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.core.db import get_sessionmaker
from app.models.daily_practice import DailyPracticeAction
from app.services import daily_practice_content as content
from app.services.daily_practice import JOB_TOP_UP
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobResult

_log = logging.getLogger("moly-worker")

MAX_ACTIONS_PER_RUN = 5
_LLM_DEADLINE_S = 45.0
_MAX_TOKENS = 4000


async def handle_top_up(job: ClaimedJob) -> JobResult:
    category = job.payload.get("category")
    slot = job.payload.get("slot")
    if category not in content.CATEGORIES or slot not in content.SLOTS:
        return JobResult(result_code="invalid_payload")
    try:
        async with get_sessionmaker()() as session:
            rows = (await session.execute(
                select(DailyPracticeAction.title_ko, DailyPracticeAction.category)
            )).all()
        batch = await asyncio.wait_for(
            content.generate(
                category,
                MAX_ACTIONS_PER_RUN,
                existing_titles=[title for title, _ in rows],
                avoid_in_prompt=[title for title, row_category in rows if row_category == category],
                slot=slot,
                purpose=JOB_TOP_UP,
                job_id=job.id,
                max_tokens=_MAX_TOKENS,
                timeout=_LLM_DEADLINE_S,
            ),
            timeout=_LLM_DEADLINE_S + 5,
        )
    except Exception as exc:  # noqa: BLE001 — 재시도 없이 끝낸다
        _log.warning("daily_practice_top_up_failed category=%s %s", category, type(exc).__name__)
        return JobResult(result_code="generation_failed", result_detail={"reason": type(exc).__name__})

    actions = batch.accepted[:MAX_ACTIONS_PER_RUN]

    async def apply(session) -> None:
        await session.execute(
            pg_insert(DailyPracticeAction)
            .values([
                {
                    "category": action.category,
                    "slots": list(action.slots),
                    "icon": action.icon,
                    "title_ko": action.title["ko"],
                    "title_en": action.title["en"],
                    "title_ja": action.title["ja"],
                    "speech_ko": action.speech["ko"],
                    "speech_en": action.speech["en"],
                    "speech_ja": action.speech["ja"],
                    "source": "top_up",
                    "prompt_version": content.PROMPT_VERSION,
                    "model": settings.model_daily_practice,
                }
                for action in actions
            ])
            .on_conflict_do_nothing(index_elements=["title_ko"])
        )

    return JobResult(
        result_detail={"category": category, "added": len(actions), "rejected": len(batch.rejected)},
        apply_domain=apply if actions else None,
    )


consumer.register(JOB_TOP_UP, handle_top_up)
