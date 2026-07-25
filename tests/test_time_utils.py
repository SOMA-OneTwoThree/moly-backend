from datetime import datetime, timezone

from app.core.time_utils import activity_date_for, reward_date_for


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


def test_reward_boundary_is_midnight():
    # 서울 2026-07-07 03:59 → 보상 경계 00:00 기준 → 기준일 07-07 (하루 경계와 달라짐)
    now_utc = datetime(2026, 7, 6, 18, 59, tzinfo=timezone.utc)  # KST 03:59
    assert reward_date_for(now_utc, "Asia/Seoul").isoformat() == "2026-07-07"


def test_reward_just_after_midnight():
    # 서울 2026-07-07 00:01 → 07-07
    now_utc = datetime(2026, 7, 6, 15, 1, tzinfo=timezone.utc)  # KST 00:01
    assert reward_date_for(now_utc, "Asia/Seoul").isoformat() == "2026-07-07"


# --- DST·30/45분 오프셋(SOMA-348): 04:00 경계가 로컬 벽시계로 정확한지 ---
def test_dst_spring_forward_new_york():
    # 미국 봄 DST(2026-03-08 02:00 EST→03:00 EDT). 전환 후 UTC-4로 경계 계산.
    # 07:30 UTC = 03:30 EDT → 04:00 미만 → 전날
    assert activity_date_for(
        datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc), "America/New_York"
    ).isoformat() == "2026-03-07"
    # 08:30 UTC = 04:30 EDT → 당일
    assert activity_date_for(
        datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc), "America/New_York"
    ).isoformat() == "2026-03-08"


def test_half_hour_offset_india():
    # 인도 +5:30 — 30분 오프셋에서도 로컬 04:00 경계 정확.
    # 2026-06-30 22:00 UTC = 07-01 03:30 IST → 전날 06-30
    assert activity_date_for(
        datetime(2026, 6, 30, 22, 0, tzinfo=timezone.utc), "Asia/Kolkata"
    ).isoformat() == "2026-06-30"
    # 2026-06-30 23:00 UTC = 07-01 04:30 IST → 당일 07-01
    assert activity_date_for(
        datetime(2026, 6, 30, 23, 0, tzinfo=timezone.utc), "Asia/Kolkata"
    ).isoformat() == "2026-07-01"


def test_three_quarter_hour_offset_nepal():
    # 네팔 +5:45 — 45분 오프셋 경계.
    # 2026-06-30 22:15 UTC = 07-01 04:00 NPT → 경계 도달 → 당일 07-01
    assert activity_date_for(
        datetime(2026, 6, 30, 22, 15, tzinfo=timezone.utc), "Asia/Kathmandu"
    ).isoformat() == "2026-07-01"
    # 2026-06-30 22:14 UTC = 07-01 03:59 NPT → 전날 06-30
    assert activity_date_for(
        datetime(2026, 6, 30, 22, 14, tzinfo=timezone.utc), "Asia/Kathmandu"
    ).isoformat() == "2026-06-30"
