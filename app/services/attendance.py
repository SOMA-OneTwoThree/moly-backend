"""출석 — 앱 진입 출석 체크(보상 지급)와 연속 출석. 날짜는 보상 기준일(현지 00:00)이다.

출석한 날은 user_daily_stats.attendance_claimed_at이 있는 날이다(이전 충전소 수령 포함). 연속 출석 전에는
루틴 연속이 그 자리였으므로, 처음 출석 체크할 때 어제까지 이어진 루틴 연속을 출석한 날로 한 번 옮겨 두고
(attendance_streak_seeds) 이후로는 루틴과 무관하게 센다. 지급량은 연속 일수를 따라 7일 주기로 돌아간다.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.advisory_lock import advisory_xact_lock
from app.core.time_utils import current_reward_date
from app.models.attendance_streak_seed import AttendanceStreakSeed
from app.models.profile import Profile
from app.models.user_daily_stats import UserDailyStats
from app.services import economy, hay_ledger, routine, routine_records
from app.services.account import _load_profile

WINDOW_MAX_DAYS = 371
REWARD_CYCLE = (20, 20, 60, 20, 20, 20, 120)
_ONE_DAY = timedelta(days=1)


def reward(streak: int) -> int:
    """연속 출석 streak일째의 지급량. 8일째부터 다시 1일째 값이다."""
    return REWARD_CYCLE[(streak - 1) % len(REWARD_CYCLE)]


def streaks(days: set[date], today: date) -> tuple[int, int]:
    """(현재, 최장). 오늘 출석 전이면 어제까지 이어진 연속이 현재다."""
    run = best = 0
    previous: date | None = None
    for day in sorted(day for day in days if day <= today):
        run = run + 1 if previous == day - _ONE_DAY else 1
        best = max(best, run)
        previous = day
    alive = previous is not None and previous >= today - _ONE_DAY
    return (run if alive else 0), best


def seed_days(seed: AttendanceStreakSeed) -> set[date]:
    return {seed.through_date - timedelta(days=offset) for offset in range(seed.streak)}


async def _routine_streak(session: AsyncSession, profile: Profile, today: date) -> tuple[int, int]:
    """(어제까지 이어진 루틴 연속, 루틴 최장 연속). 규칙은 루틴 통계 summary와 같다."""
    rows = await routine._all_routines(session, profile.id)
    records = await routine._routine_records(session, profile, rows, today)
    totals = routine_records.totals(records.values(), today)
    if not totals.scheduled:
        return 0, 0
    days = [totals.day(day) for day in routine_records.dates(min(totals.scheduled), today)]
    current, _ = routine_records.streaks(days[:-1], today)
    _, best = routine_records.streaks(days, today)
    return current, best


async def _stored_seed(session: AsyncSession, uid: uuid.UUID) -> AttendanceStreakSeed | None:
    return (
        await session.execute(select(AttendanceStreakSeed).where(AttendanceStreakSeed.user_id == uid))
    ).scalars().first()


async def _new_seed(session: AsyncSession, profile: Profile, today: date) -> AttendanceStreakSeed:
    streak, best = await _routine_streak(session, profile, today)
    return AttendanceStreakSeed(
        user_id=profile.id, through_date=today - _ONE_DAY, streak=streak, best_streak=best,
    )


async def _attended(session: AsyncSession, uid: uuid.UUID, today: date) -> set[date]:
    return set(
        (
            await session.execute(
                select(UserDailyStats.activity_date).where(
                    UserDailyStats.user_id == uid,
                    UserDailyStats.attendance_claimed_at.isnot(None),
                    UserDailyStats.activity_date <= today,
                )
            )
        ).scalars().all()
    )


def _record(attended: set[date], seed: AttendanceStreakSeed, today: date) -> tuple[set[date], int, int]:
    days = attended | seed_days(seed)
    current, best = streaks(days, today)
    return days, current, max(best, seed.best_streak)


async def check_in(session: AsyncSession, user_id: str) -> dict[str, Any]:
    """오늘 첫 출석이면 기록하고 출석 보상을 지급한다. 이미 출석했으면 그대로 현재 연속을 돌려준다."""
    profile = await _load_profile(session, user_id)
    uid = profile.id
    # 순서 불변식은 economy.claim_attendance와 같다: lock → 커서 assert → 기록 → commit.
    await advisory_xact_lock(session, uid)
    today = current_reward_date(profile.timezone)
    regressed = await economy._reward_window_regressed(session, uid, today)
    seed = await _stored_seed(session, uid)
    if seed is None:
        seed = await _new_seed(session, profile, today)
        session.add(seed)
    checked_in = False
    if not regressed:  # tz 역행 날짜에는 출석도 보상도 기록하지 않는다
        stats = await economy._daily(session, uid, today)
        if stats.attendance_claimed_at is None:
            stats.attendance_claimed_at = datetime.now(timezone.utc)
            checked_in = True
    _, current, best = _record(await _attended(session, uid, today), seed, today)
    granted, balance = 0, profile.hay_balance
    if checked_in:
        granted = reward(current)
        balance = (await hay_ledger.apply(session, uid, "attendance", granted)).balance_after
    await session.commit()
    return {
        "today": today,
        "checked_in": checked_in,
        "granted": granted,
        "balance_after": max(0, balance),
        "current_streak": current,
        "best_streak": best,
        "streak_rewards": list(REWARD_CYCLE),
    }


async def today_reward(session: AsyncSession, profile: Profile, today: date) -> int:
    """오늘 출석했거나 지금 출석하면 받는 지급량. 아직 출석 체크 전이면 옮겨 둘 루틴 연속을 미리 센다."""
    seed = await _stored_seed(session, profile.id) or await _new_seed(session, profile, today)
    _, current, _ = _record(await _attended(session, profile.id, today) | {today}, seed, today)
    return reward(current)


async def get_attendance(session: AsyncSession, user_id: str, start: date, end: date) -> dict[str, Any]:
    """현재·최장 연속과 from..to 안의 출석한 날. 아직 출석 체크 전이면 옮겨 둘 루틴 연속을 미리 센다."""
    if start > end or (end - start).days >= WINDOW_MAX_DAYS:
        raise errors.validation(f"조회 기간은 from ≤ to이고 {WINDOW_MAX_DAYS}일 이하여야 해요.")
    profile = await _load_profile(session, user_id)
    today = current_reward_date(profile.timezone)
    seed = await _stored_seed(session, profile.id) or await _new_seed(session, profile, today)
    attended = await _attended(session, profile.id, today)
    days, current, best = _record(attended, seed, today)
    return {
        "today": today,
        "attended_today": today in attended,
        "current_streak": current,
        "best_streak": best,
        "days": sorted(day for day in days if start <= day <= end),
    }
