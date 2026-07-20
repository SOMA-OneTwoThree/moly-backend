"""배치 틱 — 매시 크론이 호출(멱등). 로컬 04:00 일기 생성 / 09:00 아침·20:00 저녁 푸시."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.time_utils import activity_date_for
from app.models.profile import Profile
from app.services import diary_generation, memory, notify, slack_notify
from app.services.limits import effective_token_config

_log = logging.getLogger("moly-worker")
DIARY_HOUR = 4  # 로컬 04:00 일기 생성
MORNING_HOUR = 9  # 09:00 아침 일기 푸시
EVENING_HOUR = 20  # 20:00 저녁 안부 푸시


def _build_summary(now: datetime, counts: dict, elapsed: float) -> str:
    """워커 틱 슬랙 요약 메시지 조립."""
    has_warn = counts["diary_failed"] > 0 or counts["memory_failed"] > 0
    prefix = "⚠️ " if has_warn else ""
    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    diary_fail = f", 실패 ⚠️ {counts['diary_failed']}건" if counts["diary_failed"] else ""
    mem_fail = f" ⚠️ {counts['memory_failed']}" if counts["memory_failed"] else f" {counts['memory_failed']}"
    return "\n".join([
        f"{prefix}[워커 요약] {ts}",
        f"일기: {counts['diaries']}건 (개인 {counts['diary_llm']} / 프리셋 {counts['diary_preset']}){diary_fail}",
        f"기억(mem0): 성공 {counts['memory_ok']} / 실패{mem_fail}",
        f"푸시: 아침 {counts['morning']}건 / 저녁 {counts['evening']}건",
        f"전체 유저 {counts['users']}명 | 소요 {elapsed:.1f}s",
    ])


async def run_tick(now: datetime | None = None) -> dict[str, int]:
    """이번 틱 처리 건수(일기·아침·저녁)."""
    now = now or datetime.now(timezone.utc)
    counts = {
        "diaries": 0, "diary_llm": 0, "diary_preset": 0, "diary_failed": 0,
        "memory_ok": 0, "memory_failed": 0,
        "morning": 0, "evening": 0,
        "diary_attempted": 0,  # DIARY_HOUR에 진입한 유저 수(생성·스킵·실패 합산)
        "users": 0,
    }
    start = time.monotonic()
    async with get_sessionmaker()() as session:
        cfg = await effective_token_config(session)
        # 전 프로필 대상(닉네임 유무 무관). 온보딩 전에도 채팅이 되므로 닉네임으로 거르면
        # 대화한 유저가 일기를 영영 못 받는다. timezone은 NOT NULL(기본 Asia/Seoul)이라 안전.
        profiles = list((await session.execute(select(Profile))).scalars().all())
        counts["users"] = len(profiles)
        for p in profiles:
            hour = now.astimezone(ZoneInfo(p.timezone)).hour
            try:
                if hour == DIARY_HOUR:
                    counts["diary_attempted"] += 1
                    target = activity_date_for(now, p.timezone) - timedelta(days=1)
                    result = await diary_generation.generate_for_user(session, p, target, cfg)
                    if result.get("created"):
                        counts["diaries"] += 1
                        if result.get("source") == "llm":
                            counts["diary_llm"] += 1
                        else:
                            counts["diary_preset"] += 1
                    counts["memory_ok"] += result.get("memory_ok", 0)
                    counts["memory_failed"] += result.get("memory_failed", 0)
                elif hour == MORNING_HOUR:
                    if await notify.notify_morning(session, p):
                        counts["morning"] += 1
                elif hour == EVENING_HOUR:
                    if await notify.notify_evening(session, p):
                        counts["evening"] += 1
            except Exception as e:  # noqa: BLE001  # 한 유저 실패가 배치를 멈추지 않게
                _log.exception("틱 처리 실패(user=%s hour=%s): %r", p.id, hour, e)
                await session.rollback()  # 세션 무효화 방지 — 다음 유저 계속
                if hour == DIARY_HOUR:
                    counts["diary_failed"] += 1

        # 탈퇴 고아 기억 청소(하루 1회, UTC 04시 틱) — vecs는 FK 밖이라 CASCADE 안 닿음(백스톱)
        if now.hour == DIARY_HOUR:
            try:
                counts["swept"] = await memory.sweep_orphans(session)
            except Exception as e:  # noqa: BLE001
                _log.warning("고아 기억 스위프 실패: %r", e)
                await session.rollback()

    elapsed = time.monotonic() - start

    # 슬랙 요약: 일기 틱(DIARY_HOUR에 진입한 유저 있음) 또는 푸시 발송 있을 때만 전송(빈 틱 스팸 방지)
    if counts["diary_attempted"] + counts["morning"] + counts["evening"] > 0:
        summary = _build_summary(now, counts, elapsed)
        await slack_notify.send_summary(summary)

    return counts
