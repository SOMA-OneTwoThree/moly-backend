"""schedule 4종 due 계산 — 전환 시점에 제품 동작이 바뀌지 않아야 한다.

scheduler는 tick의 full-profile scan을 대체한다. 두 경로가 **같은 시각**을 계산하지 않으면
전환하는 순간 일기와 알림이 다른 때 나간다. 그 등가를 여기서 고정한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import push_personalization, user_schedules as us
from worker import tick


def test_hours_match_the_existing_tick_constants():
    """값이 갈라지면 전환 시점에 제품 동작이 조용히 바뀐다."""
    assert us.LOCAL_HOUR[us.KIND_DIARY_GENERATE] == tick.DIARY_HOUR
    assert us.LOCAL_HOUR[us.KIND_DIARY_MORNING] == tick.MORNING_HOUR
    assert us.LOCAL_HOUR[us.KIND_EVENING_CHECKIN] == tick.EVENING_HOUR
    assert us.LOCAL_HOUR[us.KIND_DAILY_DIGEST] == push_personalization.GEN_HOUR


def test_all_four_kinds_are_covered():
    assert set(us.KINDS) == set(us.LOCAL_HOUR)
    assert len(us.KINDS) == 4


@pytest.mark.parametrize("kind", us.KINDS)
def test_due_is_in_the_future_and_at_the_right_local_hour(kind):
    now = datetime(2026, 8, 5, 3, 17, tzinfo=timezone.utc)
    due = us.next_due(kind, "Asia/Seoul", now)
    assert due > now
    assert due.astimezone(ZoneInfo("Asia/Seoul")).hour == us.LOCAL_HOUR[kind]


def test_due_rolls_to_tomorrow_when_todays_hour_has_passed():
    seoul = ZoneInfo("Asia/Seoul")
    # KST 10:00 — 오늘 09시는 지났다.
    now = datetime(2026, 8, 5, 10, 0, tzinfo=seoul).astimezone(timezone.utc)
    due = us.next_due(us.KIND_DIARY_MORNING, "Asia/Seoul", now)
    assert due.astimezone(seoul).date() == datetime(2026, 8, 6).date()


def test_due_is_today_when_the_hour_is_still_ahead():
    seoul = ZoneInfo("Asia/Seoul")
    now = datetime(2026, 8, 5, 8, 0, tzinfo=seoul).astimezone(timezone.utc)
    due = us.next_due(us.KIND_DIARY_MORNING, "Asia/Seoul", now)
    assert due.astimezone(seoul).date() == datetime(2026, 8, 5).date()


def test_dst_spring_forward_does_not_lose_a_day():
    """존재하지 않는 벽시계 시각이 오면 건너뛰지 않고 다음 날로 민다.

    건너뛰면 그날 일기가 아예 안 나온다 — '절대 비지 않는다'는 제품 약속을 깬다.
    """
    # America/Santiago는 자정 근처에서 spring-forward가 일어나 00시가 없는 날이 있다.
    now = datetime(2026, 9, 6, 0, 0, tzinfo=timezone.utc)
    due = us.next_due(us.KIND_DIARY_GENERATE, "America/Santiago", now)
    assert due > now
    assert due.astimezone(ZoneInfo("America/Santiago")).hour == 4


def test_unknown_kind_is_rejected():
    with pytest.raises(KeyError):
        us.next_due("nope", "Asia/Seoul", datetime.now(timezone.utc))


def test_all_due_returns_one_per_kind():
    got = us.all_due("Asia/Seoul", datetime.now(timezone.utc))
    assert [d.kind for d in got] == list(us.KINDS)
    assert all(d.timezone_snapshot == "Asia/Seoul" for d in got)


def test_timezone_is_snapshotted_not_recomputed():
    """스냅샷이 없으면 여행 중 tz 변경이 이미 계산된 due를 흔든다."""
    got = us.all_due("Europe/Berlin", datetime.now(timezone.utc))
    assert {d.timezone_snapshot for d in got} == {"Europe/Berlin"}
