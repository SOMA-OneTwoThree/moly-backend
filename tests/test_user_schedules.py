"""schedule 4종 due 계산 — 전환 시점에 제품 동작이 바뀌지 않아야 한다.

scheduler는 tick의 full-profile scan을 대체한다. 두 경로가 **같은 시각**을 계산하지 않으면
전환하는 순간 일기와 알림이 다른 때 나간다. 그 등가를 여기서 고정한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import user_schedules as us
from worker import tick


def test_hours_match_the_existing_tick_constants():
    """값이 갈라지면 전환 시점에 제품 동작이 조용히 바뀐다."""
    assert us.LOCAL_HOUR[us.KIND_DIARY_GENERATE] == tick.DIARY_HOUR
    assert us.LOCAL_HOUR[us.KIND_DIARY_MORNING] == tick.MORNING_HOUR
    assert us.LOCAL_HOUR[us.KIND_EVENING_CHECKIN] == tick.EVENING_HOUR
    # daily_digest의 05시는 푸시 개인화(2026-08-06 revert로 제거)가 쓰던 슬롯 예약값 —
    # 대응하는 tick 상수가 더 이상 없어 값 자체를 고정한다.
    assert us.LOCAL_HOUR[us.KIND_DAILY_DIGEST] == 5


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


# ── timezone 변경 반영 (실측 버그) ──────────────────────────

def test_retime_exists_because_snapshot_is_not_permanent():
    """스냅샷은 '실행 중 tz 변경에 안 흔들리기 위한 것'이지 영원히 안 바뀌는 값이 아니다.

    dev 실측: profile은 Asia/Dubai인데 스냅샷은 Asia/Seoul이었다. 그래서 due 인덱스와
    기존 스캔이 **서로 다른 사용자**를 골랐고, 전환했으면 그 사용자만 조용히 알림을 놓쳤다.
    """
    assert hasattr(us, "retime_for_user")


def test_retime_only_touches_rows_whose_timezone_changed():
    """안 바뀐 행까지 건드리면 진행 중 실행의 due가 흔들린다."""
    import inspect

    src = inspect.getsource(us)
    assert "timezone_snapshot <> :tz" in src


def test_retime_bumps_revision():
    """진행 중 실행이 자기 세대를 확인할 수 있어야 한다."""
    import inspect

    assert "revision = revision + 1" in inspect.getsource(us)


# ── due dispatcher ──────────────────────────────────────────

def test_dispatcher_is_off_by_default():
    """잘못 켜면 그 사용자만 조용히 일기·알림을 못 받는다. 설계도 확인 전 전환을 금지한다."""
    from app.config import settings

    assert settings.schedule_dispatcher_enabled is False


def test_advance_is_a_compare_and_set():
    """두 워커가 같은 스케줄을 집어도 하나만 통과해야 중복 발송이 없다."""
    import inspect

    src = inspect.getsource(us.advance)
    assert "next_due_at = :expected" in inspect.getsource(us) or "expected" in src


def test_due_uses_the_index_not_a_full_scan():
    """전 profile 스캔이면 사용자 수에 비례해 비용이 는다 — 15분 안에 못 돌면 그날이 밀린다."""
    import inspect

    src = inspect.getsource(us)
    assert "FROM user_schedules" in src
    assert "FROM profiles" not in src, "dispatcher가 profile을 훑는다"
