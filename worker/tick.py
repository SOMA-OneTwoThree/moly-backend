"""배치 틱 — 매시 기억 추출 / 로컬 04:00 일기 / 09:00·20:00 푸시."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.core.db import get_sessionmaker
from app.core.time_utils import activity_date_for
from app.models.profile import Profile
from app.services import diary_generation, memory, memory_ingestion, notify
from app.services.limits import effective_token_config

_log = logging.getLogger("moly-worker")
DIARY_HOUR = 4  # 로컬 04:00 일기 생성
MORNING_HOUR = 9  # 09:00 아침 일기 푸시
EVENING_HOUR = 20  # 20:00 저녁 안부 푸시


async def run_tick(now: datetime | None = None) -> dict[str, int]:
    """이번 틱 처리 건수(일기·기억·아침·저녁)."""
    now = now or datetime.now(timezone.utc)
    counts = {"diaries": 0, "morning": 0, "evening": 0, "memory_ingestions": 0}
    if settings.memory_ingestion_enabled:
        settings.require_memory_provider_ready()
    async with get_sessionmaker()() as session:
        cfg = await effective_token_config(session)
        # 전 프로필 대상(닉네임 유무 무관). 온보딩 전에도 채팅이 되므로 닉네임으로 거르면
        # 대화한 유저가 일기를 영영 못 받는다. timezone은 NOT NULL(기본 Asia/Seoul)이라 안전.
        profiles = list((await session.execute(select(Profile))).scalars().all())
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

        if settings.memory_ingestion_enabled:
            try:
                counts["memory_ingestions"] = await memory_ingestion.ingest_pending(session, now)
            except Exception as e:  # noqa: BLE001
                _log.exception("기억 추출 배치 실패: %r", e)
                await session.rollback()
                raise

        # 탈퇴 고아 기억 청소(하루 1회, UTC 04시 틱) — vecs는 FK 밖이라 CASCADE 안 닿음(백스톱)
        if now.hour == DIARY_HOUR:
            try:
                counts["swept"] = await memory.sweep_orphans(session)
            except Exception as e:  # noqa: BLE001
                _log.warning("고아 기억 스위프 실패: %r", e)
                await session.rollback()
    return counts
