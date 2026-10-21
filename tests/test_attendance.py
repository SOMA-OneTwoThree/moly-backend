"""연속 출석 — 연속 계산, 루틴 연속 이어받기, 출석 체크 멱등성, 조회 기간(DB 없음)."""
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.models.attendance_streak_seed import AttendanceStreakSeed
from app.models.user_daily_stats import UserDailyStats
from app.services import attendance, economy, hay_ledger, routine
from app.services.routine_records import RoutineRecord
from tests.fake_db import FakeDbSession

UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER = uuid.UUID("22222222-2222-2222-2222-222222222222")
TODAY = date(2026, 10, 22)  # 목요일
WEEKDAYS = (1, 2, 3, 4, 5)


def _days(*offsets: int) -> set[date]:
    return {TODAY - timedelta(days=offset) for offset in offsets}


# --- 연속 계산 ---
@pytest.mark.parametrize(("days", "expected"), [
    (set(), (0, 0)),
    (_days(0, 1, 2), (3, 3)),
    (_days(1, 2), (2, 2)),  # 오늘 출석 전 — 어제까지의 연속이 살아 있다
    (_days(2, 3, 4), (0, 3)),  # 어제를 놓쳤다
    (_days(0, 1, 3, 4, 5, 6), (2, 4)),
    (_days(0) | {TODAY + timedelta(days=1)}, (1, 1)),  # 미래 날짜는 세지 않는다
])
def test_streaks(days, expected):
    assert attendance.streaks(days, TODAY) == expected


def test_seed_days_end_at_through_date():
    seed = AttendanceStreakSeed(through_date=TODAY - timedelta(days=1), streak=3, best_streak=5)
    assert attendance.seed_days(seed) == _days(1, 2, 3)
    assert attendance.seed_days(AttendanceStreakSeed(through_date=TODAY, streak=0, best_streak=0)) == set()


# --- 루틴 연속 이어받기 ---
def _patch_routines(monkeypatch, *records: RoutineRecord):
    async def rows(session, uid):
        return [SimpleNamespace(id=index) for index in range(len(records))]

    async def routine_records(session, profile, rows, until):
        return dict(enumerate(records))

    monkeypatch.setattr(routine, "_all_routines", rows)
    monkeypatch.setattr(routine, "_routine_records", routine_records)


def _weekday_routine(*completed_offsets: int) -> RoutineRecord:
    return RoutineRecord(
        versions=((TODAY - timedelta(days=60), WEEKDAYS),), end=None,
        completed=frozenset(_days(*completed_offsets)),
    )


async def test_routine_streak_carries_over_the_weekend_and_skips_today(monkeypatch):
    # 지난주 목·금, 이번 주 월·화·수 완료. 토·일은 예정이 없어 건너뛰고, 오늘 완료는 출석 체크가 센다.
    _patch_routines(monkeypatch, _weekday_routine(0, 1, 2, 3, 6, 7))
    profile = SimpleNamespace(id=UID)
    assert await attendance._routine_streak(None, profile, TODAY) == (5, 6)


async def test_routine_streak_breaks_on_a_missed_scheduled_day(monkeypatch):
    _patch_routines(monkeypatch, _weekday_routine(2, 3))  # 어제(수) 예정이었는데 안 했다
    assert await attendance._routine_streak(None, SimpleNamespace(id=UID), TODAY) == (0, 2)


async def test_routine_streak_without_routines(monkeypatch):
    _patch_routines(monkeypatch)
    assert await attendance._routine_streak(None, SimpleNamespace(id=UID), TODAY) == (0, 0)


# --- 출석 체크 ---
class _Session(FakeDbSession):
    def __init__(self, tables):
        super().__init__(tables)
        self.committed = False

    def add(self, obj):
        self.tables.setdefault(type(obj), []).append(obj)

    async def commit(self):
        self.committed = True


def _stats(offset: int, user_id=UID, claimed=True):
    return SimpleNamespace(
        user_id=user_id, activity_date=TODAY - timedelta(days=offset),
        attendance_claimed_at=datetime(2026, 10, 1, tzinfo=timezone.utc) if claimed else None,
    )


@pytest.fixture
def check_in_env(monkeypatch):
    calls = SimpleNamespace(grants=[], routine_streaks=0, regressed=False)

    async def profile(session, user_id):
        return SimpleNamespace(id=UID, timezone="Asia/Seoul", hay_balance=640)

    async def lock(session, uid):
        pass

    async def regressed(session, uid, reward_date):
        return calls.regressed

    async def daily(session, uid, activity_date):
        rows = session.tables.setdefault(UserDailyStats, [])
        for row in rows:
            if row.user_id == uid and row.activity_date == activity_date:
                return row
        row = SimpleNamespace(user_id=uid, activity_date=activity_date, attendance_claimed_at=None)
        rows.append(row)
        return row

    async def apply(session, uid, tx_type, amount, **kw):
        calls.grants.append((tx_type, amount))
        return SimpleNamespace(balance_after=640 + amount)

    async def routine_streak(session, profile, today):
        calls.routine_streaks += 1
        return 4, 9

    monkeypatch.setattr(attendance, "_load_profile", profile)
    monkeypatch.setattr(attendance, "current_reward_date", lambda tz: TODAY)
    monkeypatch.setattr(attendance, "advisory_xact_lock", lock)
    monkeypatch.setattr(economy, "_reward_window_regressed", regressed)
    monkeypatch.setattr(economy, "_daily", daily)
    monkeypatch.setattr(hay_ledger, "apply", apply)
    monkeypatch.setattr(attendance, "_routine_streak", routine_streak)
    return calls


async def test_first_check_in_seeds_the_routine_streak_and_grants_hay(check_in_env):
    session = _Session({UserDailyStats: [_stats(6), _stats(1, user_id=OTHER)]})
    out = await attendance.check_in(session, str(UID))
    assert out == {
        "today": TODAY, "checked_in": True, "granted": 20, "balance_after": 660,
        "current_streak": 5, "best_streak": 9,
    }
    [seed] = session.tables[AttendanceStreakSeed]
    assert (seed.user_id, seed.through_date, seed.streak, seed.best_streak) == (UID, TODAY - timedelta(days=1), 4, 9)
    assert check_in_env.grants == [("attendance", 20)]
    assert session.committed


async def test_second_check_in_is_idempotent(check_in_env):
    session = _Session({UserDailyStats: [_stats(0), _stats(1)]})
    session.tables[AttendanceStreakSeed] = [
        AttendanceStreakSeed(user_id=UID, through_date=TODAY - timedelta(days=5), streak=2, best_streak=0),
    ]
    out = await attendance.check_in(session, str(UID))
    assert out == {
        "today": TODAY, "checked_in": False, "granted": 0, "balance_after": 640,
        "current_streak": 2, "best_streak": 2,
    }
    assert check_in_env.grants == []
    assert check_in_env.routine_streaks == 0  # 이어받기는 한 번만 센다
    assert len(session.tables[AttendanceStreakSeed]) == 1


async def test_check_in_on_a_regressed_date_records_nothing(check_in_env):
    check_in_env.regressed = True
    session = _Session({UserDailyStats: [_stats(1)]})
    out = await attendance.check_in(session, str(UID))
    assert (out["checked_in"], out["granted"], out["current_streak"]) == (False, 0, 4)
    assert check_in_env.grants == []
    assert all(row.activity_date != TODAY for row in session.tables[UserDailyStats])


async def test_check_in_after_a_charging_station_claim_does_not_grant_again(check_in_env):
    session = _Session({UserDailyStats: [_stats(0)]})
    out = await attendance.check_in(session, str(UID))
    assert (out["checked_in"], out["granted"]) == (False, 0)
    assert check_in_env.grants == []


# --- 조회 ---
async def test_attendance_reports_the_window_without_storing_a_seed(check_in_env):
    session = _Session({UserDailyStats: [
        _stats(0), _stats(10), _stats(40), _stats(3, claimed=False), _stats(2, user_id=OTHER),
    ]})
    out = await attendance.get_attendance(session, str(UID), TODAY - timedelta(days=30), TODAY)
    # 이어받을 루틴 연속 4일(어제까지)과 오늘 출석이 이어진다. 기간 밖 40일 전은 days에서 빠진다.
    assert out == {
        "today": TODAY, "attended_today": True, "current_streak": 5, "best_streak": 9,
        "days": sorted(_days(0, 1, 2, 3, 4, 10)),
    }
    assert AttendanceStreakSeed not in session.tables


async def test_attendance_before_today_s_check_in(check_in_env):
    session = _Session({UserDailyStats: [_stats(1)]})
    session.tables[AttendanceStreakSeed] = [
        AttendanceStreakSeed(user_id=UID, through_date=TODAY - timedelta(days=9), streak=0, best_streak=0),
    ]
    out = await attendance.get_attendance(session, str(UID), TODAY, TODAY)
    assert (out["attended_today"], out["current_streak"], out["best_streak"], out["days"]) == (False, 1, 1, [])


@pytest.mark.parametrize(("start", "end"), [
    (TODAY, TODAY - timedelta(days=1)),
    (TODAY - timedelta(days=371), TODAY),
])
async def test_attendance_rejects_invalid_windows(start, end):
    with pytest.raises(AppError) as e:
        await attendance.get_attendance(None, str(UID), start, end)
    assert e.value.code == "VALIDATION"


# --- API 표면 ---
@pytest.fixture
def client():
    async def session():
        yield SimpleNamespace()

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = lambda: str(UID)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_attendance_routes(client, monkeypatch):
    async def get_attendance(session, user_id, start, end):
        assert (user_id, start, end) == (str(UID), date(2026, 10, 1), date(2026, 10, 31))
        return {"today": TODAY, "attended_today": True, "current_streak": 3, "best_streak": 7, "days": [TODAY]}

    async def check_in(session, user_id):
        return {
            "today": TODAY, "checked_in": True, "granted": 20, "balance_after": 660,
            "current_streak": 3, "best_streak": 7,
        }

    monkeypatch.setattr(attendance, "get_attendance", get_attendance)
    monkeypatch.setattr(attendance, "check_in", check_in)
    record = client.get("/attendance", params={"from": "2026-10-01", "to": "2026-10-31"})
    assert record.status_code == 200
    assert record.headers["cache-control"] == "private, no-store"
    assert record.json() == {
        "today": "2026-10-22", "attended_today": True, "current_streak": 3, "best_streak": 7,
        "days": ["2026-10-22"],
    }
    checked = client.post("/attendance/check-in")
    assert checked.status_code == 200
    assert checked.json() == {
        "today": "2026-10-22", "checked_in": True, "granted": 20, "balance_after": 660,
        "current_streak": 3, "best_streak": 7,
    }
    assert client.get("/attendance", params={"from": "2026-10-01"}).status_code == 422
