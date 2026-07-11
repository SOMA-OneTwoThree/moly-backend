"""배치 틱 — 매시 크론이 호출(멱등). 로컬 04:00 일기 생성 / 09:00 아침·21:00 저녁 푸시."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.time_utils import activity_date_for
from app.models.profile import Profile
from app.services import diary_generation, memory, notify
from app.services.limits import effective_token_config

_log = logging.getLogger("moly-worker")
DIARY_HOUR = 4  # 로컬 04:00 일기 생성
MORNING_HOUR = 9  # 09:00 아침 일기 푸시
EVENING_HOUR = 21  # 21:00 저녁 안부 푸시


async def run_tick(now: datetime | None = None) -> dict[str, int]:
    """이번 틱 처리 건수(일기·아침·저녁)."""
    now = now or datetime.now(timezone.utc)
    counts = {"diaries": 0, "morning": 0, "evening": 0}
    async with get_sessionmaker()() as session:
        cfg = await effective_token_config(session)
        profiles = list(
            (
                await session.execute(select(Profile).where(Profile.nickname.is_not(None)))
            ).scalars().all()
        )
        for p in profiles:
            hour = now.astimezone(ZoneInfo(p.timezone)).hour
            try:
                if hour == DIARY_HOUR:
                    target = activity_date_for(now, p.timezone) - timedelta(days=1)
                    await diary_generation.generate_for_user(session, p, target, cfg)
                    counts["diaries"] += 1
                elif hour == MORNING_HOUR:
                    if await notify.notify_morning(session, p):
                        counts["morning"] += 1
                elif hour == EVENING_HOUR:
                    if await notify.notify_evening(session, p):
                        counts["evening"] += 1
            except Exception as e:  # noqa: BLE001  # 한 유저 실패가 배치를 멈추지 않게
                _log.exception("틱 처리 실패(user=%s hour=%s): %r", p.id, hour, e)
                await session.rollback()  # 세션 무효화 방지 — 다음 유저 계속

        # 탈퇴 고아 기억 청소(하루 1회, UTC 04시 틱) — vecs는 FK 밖이라 CASCADE 안 닿음(백스톱)
        if now.hour == DIARY_HOUR:
            try:
                counts["swept"] = await memory.sweep_orphans(session)
            except Exception as e:  # noqa: BLE001
                _log.warning("고아 기억 스위프 실패: %r", e)
                await session.rollback()
    return counts
