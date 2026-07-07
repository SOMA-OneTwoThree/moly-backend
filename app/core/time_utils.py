"""앱 기준일(activity_date) 계산 — ERD §1.2.

모든 일 단위 로직(토큰 리셋·출석·광고·루틴·일기 귀속)의 날짜 키.
= (유저 로컬 현재시각 − 4시간)::date. 유저 타임존(IANA)은 profiles.timezone.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# 하루 경계 = 유저 로컬 오전 04:00.
DAY_BOUNDARY_HOUR = 4


def activity_date_for(now_utc: datetime, tz_name: str) -> date:
    """주어진 UTC 시각·타임존에서의 앱 기준일."""
    local = now_utc.astimezone(ZoneInfo(tz_name))
    return (local - timedelta(hours=DAY_BOUNDARY_HOUR)).date()


def current_activity_date(tz_name: str) -> date:
    """현재 시각 기준 앱 기준일."""
    return activity_date_for(datetime.now(timezone.utc), tz_name)
