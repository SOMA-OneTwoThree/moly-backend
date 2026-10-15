"""레거시 IANA 별칭 회귀 — 운영 DB에 실재하는 값(Android의 Asia/Calcutta, iOS의 US/Eastern 등).

python:3.12-slim(trixie)은 별칭 링크를 tzdata-legacy로 분리해 시스템 zoneinfo만으로는 못 푼다.
OS 시스템 경로를 끄고(tzpath=[]) 파이썬 tzdata 패키지만으로 풀리는지 고정한다 —
macOS 개발기·CI처럼 시스템에 별칭이 있는 환경에서도 이 의존성 누락을 잡아낸다.
"""
import zoneinfo
from datetime import datetime, timezone

import pytest

from app.core.time_utils import activity_date_for, is_valid_iana_timezone

LEGACY_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "US/Eastern": "America/New_York",
    "US/Pacific": "America/Los_Angeles",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Europe/Kiev": "Europe/Kyiv",
}


@pytest.fixture
def tzdata_package_only():
    zoneinfo.reset_tzpath([])  # 시스템 TZPATH 비활성 → tzdata 패키지만
    zoneinfo.ZoneInfo.clear_cache()
    try:
        yield
    finally:
        zoneinfo.reset_tzpath()
        zoneinfo.ZoneInfo.clear_cache()


@pytest.mark.parametrize("alias,canonical", sorted(LEGACY_ALIASES.items()))
def test_legacy_alias_resolves_without_system_tzdata(tzdata_package_only, alias, canonical):
    assert is_valid_iana_timezone(alias)
    when = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert (when.astimezone(zoneinfo.ZoneInfo(alias)).utcoffset()
            == when.astimezone(zoneinfo.ZoneInfo(canonical)).utcoffset())


def test_calcutta_day_boundary_is_ist_not_kst_fallback(tzdata_package_only):
    # IST 2026-10-01 01:30 (= KST 05:00) → IST 기준 04:00 경계 이전이라 09-30. KST 폴백이면 10-01.
    now = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
    assert activity_date_for(now, "Asia/Calcutta").isoformat() == "2026-09-30"
