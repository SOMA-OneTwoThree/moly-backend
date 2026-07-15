"""앱 기준일(activity_date) 계산 — ERD §1.2.

두 종류의 날짜 키. 유저 타임존(IANA)은 profiles.timezone.
- activity_date = (유저 로컬 현재시각 − 4시간)::date — 토큰 리셋·일기 귀속(하루 경계 04:00).
- reward_date   = 유저 로컬 현재시각::date — 출석·루틴·광고 보상(보상 경계 00:00).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# 하루 경계 = 유저 로컬 오전 04:00 (토큰 리셋·일기 귀속).
DAY_BOUNDARY_HOUR = 4
# 보상 경계 = 유저 로컬 자정 00:00 (출석·루틴·광고).
REWARD_BOUNDARY_HOUR = 0


def activity_date_for(now_utc: datetime, tz_name: str, *, boundary_hour: int = DAY_BOUNDARY_HOUR) -> date:
    """주어진 UTC 시각·타임존에서 boundary_hour 경계 기준의 날짜."""
    local = now_utc.astimezone(ZoneInfo(tz_name))
    return (local - timedelta(hours=boundary_hour)).date()


def current_activity_date(tz_name: str) -> date:
    """현재 시각 기준 앱 기준일(하루 경계 04:00)."""
    return activity_date_for(datetime.now(timezone.utc), tz_name)


def reward_date_for(now_utc: datetime, tz_name: str) -> date:
    """주어진 UTC 시각·타임존에서의 보상 기준일(보상 경계 00:00)."""
    return activity_date_for(now_utc, tz_name, boundary_hour=REWARD_BOUNDARY_HOUR)


def current_reward_date(tz_name: str) -> date:
    """현재 시각 기준 보상 기준일(보상 경계 00:00)."""
    return reward_date_for(datetime.now(timezone.utc), tz_name)
