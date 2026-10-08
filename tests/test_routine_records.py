"""루틴 기록 계산 규칙 — 요일 이력·삭제·스킵·레거시 완료와 스트릭·달성률 경계."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.services import routine, routine_records as rr
from app.services.routine_records import RoutineRecord

TODAY = date(2026, 10, 8)  # 목요일
ALL = (1, 2, 3, 4, 5, 6, 7)


def d(n: int) -> date:
    return TODAY - timedelta(days=n)


def record(start, days=ALL, *, end=None, completed=(), skips=(), versions=None):
    return RoutineRecord(
        versions=versions or ((start, tuple(days)),),
        end=end,
        completed=frozenset(completed),
        skips=frozenset(skips),
    )


def test_schedule_without_history_starts_on_created_date_with_current_days():
    assert rr.schedule([], [3, 1, 1], date(2026, 1, 2), date(2026, 3, 1)) == (
        (date(2026, 1, 2), (1, 3)),
    )


def test_schedule_keeps_matching_history_in_date_order():
    assert rr.schedule([(d(1), [2]), (d(10), [3, 1])], [2], d(20), d(1)) == (
        (d(10), (1, 3)),
        (d(1), (2,)),
    )


def test_schedule_reconciles_days_changed_without_history():
    stored = [(d(10), [1])]
    assert rr.schedule(stored, [4, 2], d(10), d(3)) == ((d(10), (1,)), (d(3), (2, 4)))
    assert rr.schedule(stored, [4, 2], d(10), d(10)) == ((d(10), (2, 4)),)
    assert rr.schedule(stored, [4, 2], d(10), None) == ((d(10), (2, 4)),)


def test_reschedule_keeps_past_versions_and_replaces_today_and_later():
    versions = ((d(10), (1,)), (TODAY, (2,)), (TODAY + timedelta(days=1), (3,)))
    assert rr.reschedule(versions, TODAY, [5, 4]) == [(d(10), (1,)), (TODAY, (4, 5))]
    assert rr.reschedule(((TODAY + timedelta(days=1), (3,)),), TODAY, [1]) == [(TODAY, (1,))]


def test_days_change_applies_from_its_own_day():
    changed = record(None, versions=((d(14), ALL), (d(2), (4,))))
    assert not changed.planned(d(15))
    assert changed.planned(d(14))
    assert changed.planned(d(3)) and changed.days_on(d(3)) == ALL
    assert not changed.planned(d(2)) and changed.days_on(d(2)) == (4,)
    assert not changed.planned(d(1))
    assert changed.planned(TODAY)


def test_deletion_day_is_no_longer_planned_but_its_completion_counts():
    deleted = record(d(14), end=TODAY)
    assert deleted.planned(d(1))
    assert not deleted.planned(TODAY) and not deleted.is_instance(TODAY)
    finished_before_delete = record(d(14), end=TODAY, completed={TODAY})
    assert finished_before_delete.is_instance(TODAY)
    assert TODAY in finished_before_delete.instance_dates(TODAY)
    assert d(1) in finished_before_delete.instance_dates(TODAY)


def test_legacy_completion_and_completion_before_start_are_instances():
    mondays = record(d(5), days=(1,), completed={d(1), d(7)})
    assert not mondays.planned(d(1)) and mondays.is_instance(d(1))
    assert mondays.instance_dates(TODAY) == {d(7), d(3), d(1)}


def test_skip_removes_the_instance_but_a_completion_wins():
    skipped = record(d(5), skips={d(1), d(2), TODAY}, completed={d(2)})
    assert skipped.skipped(d(1)) and not skipped.is_instance(d(1))
    assert not skipped.skipped(d(2)) and skipped.is_instance(d(2))
    assert skipped.skipped(TODAY) and not skipped.is_instance(TODAY)
    assert skipped.instance_dates(TODAY) == {d(5), d(4), d(3), d(2)}
    unplanned = record(d(5), days=(1,), skips={d(1)})
    assert not unplanned.skipped(d(1)) and not unplanned.is_instance(d(1))


def _summary(*records):
    return rr.summary(rr.totals(records, TODAY), TODAY)


def test_overall_streak_skips_unfinished_today_and_days_without_instances():
    result = _summary(record(d(6), completed={d(6), d(5), d(3), d(2), d(1)}, skips={d(4)}))
    assert (result["current_streak"], result["best_streak"]) == (5, 5)


def test_overall_streak_stops_at_a_missed_past_day():
    result = _summary(record(d(6), completed={d(5), d(3)}))
    assert (result["current_streak"], result["best_streak"]) == (0, 1)


def test_overall_streak_spans_deleted_routines_and_counts_finished_today():
    deleted = record(d(10), end=d(3), completed={d(n) for n in range(4, 11)})
    current = record(d(2), completed={d(2), d(1), TODAY})
    result = _summary(deleted, current)
    assert (result["current_streak"], result["best_streak"]) == (10, 10)


def test_day_with_only_skipped_instances_does_not_break_the_streak():
    result = _summary(record(d(3), completed={d(3), d(1)}, skips={d(2)}))
    assert (result["current_streak"], result["best_streak"]) == (2, 2)
    days = rr.totals([record(d(3), completed={d(3), d(1)}, skips={d(2)})], TODAY)
    assert days.day(d(2)) == (d(2), 0, 0)


def test_per_routine_streak_skips_days_the_routine_was_not_planned():
    mon_thu = (1, 4)
    assert rr.routine_summary(record(d(13), mon_thu, completed={d(10), d(7), d(3)}), TODAY)[
        "current_streak"
    ] == 3
    assert rr.routine_summary(record(d(13), mon_thu, completed={d(10), d(7), d(3), TODAY}), TODAY)[
        "current_streak"
    ] == 4
    broken = rr.routine_summary(record(d(13), mon_thu, completed={d(10), d(3)}), TODAY)
    assert (broken["current_streak"], broken["best_streak"]) == (1, 1)
    skipped = rr.routine_summary(record(d(13), mon_thu, completed={d(10), d(3)}, skips={d(7)}), TODAY)
    assert (skipped["current_streak"], skipped["best_streak"]) == (2, 2)


def test_rate_counts_today_only_when_completed():
    assert rr.routine_summary(record(d(2), completed={d(2), TODAY}), TODAY)["overall_rate"] == 2 / 3
    assert rr.routine_summary(record(d(2), completed={d(2)}), TODAY)["overall_rate"] == 0.5
    assert rr.routine_summary(record(TODAY), TODAY)["overall_rate"] is None


def test_summary_counts_perfect_days_totals_month_and_rates():
    daily = record(date(2026, 9, 28), completed={date(2026, 9, 29), date(2026, 9, 30),
                                                  date(2026, 10, 1), date(2026, 10, 2), TODAY})
    thursdays = record(date(2026, 10, 1), (4,), completed={date(2026, 10, 1)})
    assert _summary(daily, thursdays) == {
        "current_streak": 1,
        "best_streak": 4,
        "perfect_days": 4,
        "total_completed": 6,
        "completed_this_month": 4,
        "overall_rate": 6 / 12,
        "monthly_rate": 4 / 9,
    }
    assert rr.routine_summary(thursdays, TODAY) == {
        "current_streak": 1,
        "best_streak": 1,
        "total_completed": 1,
        "completed_this_month": 1,
        "overall_rate": 1.0,
    }


def test_summary_without_routines_is_empty():
    assert _summary() == {
        "current_streak": 0,
        "best_streak": 0,
        "perfect_days": 0,
        "total_completed": 0,
        "completed_this_month": 0,
        "overall_rate": None,
        "monthly_rate": None,
    }


def test_future_completion_is_ignored_until_its_day():
    result = _summary(record(d(1), completed={d(1), TODAY + timedelta(days=1)}))
    assert result["total_completed"] == 1
    assert rr.totals([record(d(1), completed={TODAY + timedelta(days=1)})], TODAY).completed == {}


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _QueueSession:
    def __init__(self, *results):
        self.results = list(results)

    async def execute(self, stmt):
        return _Rows(self.results.pop(0))


def _row(**over):
    base = dict(
        id=uuid.uuid4(), days_of_week=[1, 2, 3, 4, 5, 6, 7], deleted_on=None, deleted_at=None,
        created_at=datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc),
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(("zone", "start"), [
    ("Asia/Seoul", date(2026, 1, 2)),
    ("America/Los_Angeles", date(2026, 1, 1)),
    ("Not/AZone", date(2026, 1, 2)),
])
async def test_missing_history_and_deleted_on_use_the_profile_time_zone(zone, start):
    legacy = _row(deleted_at=datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc))
    profile = SimpleNamespace(id=uuid.uuid4(), timezone=zone)
    records = await routine._routine_records(_QueueSession([], [], []), profile, [legacy], TODAY)
    expected_end = date(2026, 2, 1) if zone == "America/Los_Angeles" else date(2026, 2, 2)
    assert records[legacy.id].versions == ((start, ALL),)
    assert records[legacy.id].end == expected_end


async def test_stored_history_deleted_on_completions_and_skips_win_over_fallbacks():
    current = _row(days_of_week=[2], deleted_on=date(2026, 3, 1),
                   deleted_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
                   updated_at=datetime(2026, 2, 10, 3, tzinfo=timezone.utc))
    other = _row()
    profile = SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Seoul")
    session = _QueueSession(
        [(current.id, date(2026, 1, 5), [1, 3])],
        [(current.id, date(2026, 1, 5)), (other.id, date(2026, 1, 6))],
        [(current.id, date(2026, 1, 7))],
    )
    records = await routine._routine_records(session, profile, [current, other], TODAY)
    assert records[current.id].versions == ((date(2026, 1, 5), (1, 3)), (date(2026, 2, 10), (2,)))
    assert records[current.id].end == date(2026, 3, 1)
    assert records[current.id].completed == {date(2026, 1, 5)}
    assert records[current.id].skips == {date(2026, 1, 7)}
    assert records[other.id].completed == {date(2026, 1, 6)} and records[other.id].skips == frozenset()
