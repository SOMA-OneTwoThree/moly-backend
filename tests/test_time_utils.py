from datetime import datetime, timezone

from app.core.time_utils import activity_date_for


def test_before_4am_belongs_to_previous_day():
    # 서울 2026-07-07 03:59 → 04:00 경계 미만 → 기준일 07-06
    now_utc = datetime(2026, 7, 6, 18, 59, tzinfo=timezone.utc)  # KST 03:59
    assert activity_date_for(now_utc, "Asia/Seoul").isoformat() == "2026-07-06"


def test_after_4am_belongs_to_same_day():
    # 서울 2026-07-07 04:01 → 기준일 07-07
    now_utc = datetime(2026, 7, 6, 19, 1, tzinfo=timezone.utc)  # KST 04:01
    assert activity_date_for(now_utc, "Asia/Seoul").isoformat() == "2026-07-07"


def test_timezone_matters():
    now_utc = datetime(2026, 7, 6, 19, 30, tzinfo=timezone.utc)
    seoul = activity_date_for(now_utc, "Asia/Seoul")  # KST 04:30 → 07-07
    utc = activity_date_for(now_utc, "UTC")  # 19:30 → 07-06
    assert seoul.isoformat() == "2026-07-07"
    assert utc.isoformat() == "2026-07-06"
