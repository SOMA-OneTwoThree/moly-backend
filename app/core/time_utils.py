"""앱 기준일(activity_date) 계산 — ERD §1.2.

두 종류의 날짜 키. 유저 타임존(IANA)은 profiles.timezone.
- activity_date = (유저 로컬 현재시각 − 4시간)::date — 토큰 리셋·일기 귀속(하루 경계 04:00).
- reward_date   = 유저 로컬 현재시각::date — 출석·루틴·광고 보상(보상 경계 00:00).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_log = logging.getLogger("moly-backend")

# 하루 경계 = 유저 로컬 오전 04:00 (토큰 리셋·일기 귀속).
DAY_BOUNDARY_HOUR = 4
# 보상 경계 = 유저 로컬 자정 00:00 (출석·루틴·광고).
REWARD_BOUNDARY_HOUR = 0

# 저장된 타임존이 손상됐을 때의 폴백(컬럼 기본값과 동일). 국내 유저가 대부분이라 KST 기준이 가장 안전.
_FALLBACK_TZ = "Asia/Seoul"


def is_valid_iana_timezone(name: str) -> bool:
    """IANA 타임존 유효성. moly-auth 온보딩/변경 경로가 저장 전 422 검증에 쓰도록 공개(SOMA-375)."""
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        # ValueError=상대경로/NUL 등, TypeError=str 아님. 저장 표면의 어떤 잘못된 값도 여기서 잡는다.
        return False


def safe_zone(tz_name: str) -> ZoneInfo:
    """저장된 타임존 → ZoneInfo. 손상값이면 Asia/Seoul 폴백 + 경고(저장값은 요청입력이 아니라
    매 요청 422로 막을 수 없다 → 500 대신 폴백으로 서비스를 지킨다, SOMA-375). 쓰기 검증은 moly-auth."""
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        _log.warning("잘못된 저장 타임존 → %s 폴백: %r", _FALLBACK_TZ, tz_name)
        return ZoneInfo(_FALLBACK_TZ)


def activity_date_for(now_utc: datetime, tz_name: str, *, boundary_hour: int = DAY_BOUNDARY_HOUR) -> date:
    """주어진 UTC 시각·타임존에서 boundary_hour 경계 기준의 날짜."""
    local = now_utc.astimezone(safe_zone(tz_name))
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
