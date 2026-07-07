"""배치 틱 — 매시 크론이 호출(멱등). 로컬 04:00 창을 넘긴 유저의 전일 일기 생성.

09:00 발행·아침 푸시, 21:00 저녁 푸시는 후속(APNs 인프라 필요).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.time_utils import activity_date_for
from app.models.profile import Profile
from app.services import diary_generation
from app.services.limits import effective_token_config

_log = logging.getLogger("moly-worker")
DIARY_HOUR = 4  # 로컬 04:00 창(매시 틱 = 시 단위 판별)


async def run_tick(now: datetime | None = None) -> int:
    """이번 틱에 일기 생성한 유저 수 반환."""
    now = now or datetime.now(timezone.utc)
    processed = 0
    async with get_sessionmaker()() as session:
        cfg = await effective_token_config(session)
        profiles = list(
            (
                await session.execute(select(Profile).where(Profile.nickname.is_not(None)))
            ).scalars().all()
        )
        for p in profiles:
            if now.astimezone(ZoneInfo(p.timezone)).hour != DIARY_HOUR:
                continue
            target_date = activity_date_for(now, p.timezone) - timedelta(days=1)
            try:
                await diary_generation.generate_for_user(session, p, target_date, cfg)
                processed += 1
            except Exception as e:  # noqa: BLE001  # 한 유저 실패가 배치를 멈추지 않게
                _log.exception("일기 생성 실패(user=%s): %r", p.id, e)
    return processed
