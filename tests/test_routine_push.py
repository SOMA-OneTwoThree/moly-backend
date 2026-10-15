"""루틴 푸시 — 09시 루틴 아침 대상·문구, 스트릭·어제 깨진 스트릭 계산(DB·FCM mock).

루틴 기록 계산은 앱 기록 화면과 같은 routine_records를 쓰므로, 여기서는 루틴 조회만 가짜로 두고
실제 RoutineRecord로 경우를 만든다. 날짜는 보상일(현지 0시 경계)이다.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import notify, push, routine
from app.services.routine_records import RoutineRecord

TODAY = date(2026, 10, 14)  # 수요일
NOW = datetime(2026, 10, 14, 0, 30, tzinfo=timezone.utc)  # KST 09:30
DAILY = (1, 2, 3, 4, 5, 6, 7)


def _d(n: int) -> date:
    return TODAY - timedelta(days=n)


def _record(*, done=(), skips=(), days=DAILY, start=60, end=None) -> RoutineRecord:
    return RoutineRecord(
        versions=((_d(start), tuple(days)),), end=end,
        completed=frozenset(_d(n) for n in done), skips=frozenset(_d(n) for n in skips),
    )


class _Session:
    """마지막 완료일 조회(scalar)와 savepoint만 있는 세션."""

    def __init__(self, latest=None):
        self.latest = latest

    def begin_nested(self):
        class _CM:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *a):
                return False

        return _CM()

    async def execute(self, *a, **k):
        return SimpleNamespace(scalar=lambda: self.latest)

    async def commit(self):
        pass


def _profile(**over):
    p = {"id": uuid.uuid4(), "timezone": "Asia/Seoul", "language": "ko"}
    p.update(over)
    return SimpleNamespace(**p)


def _patch_routines(monkeypatch, records):
    rows = [SimpleNamespace(id=i) for i in range(len(records))]
    monkeypatch.setattr(routine, "_all_routines", AsyncMock(return_value=rows))
    monkeypatch.setattr(
        routine, "_routine_records", AsyncMock(return_value=dict(enumerate(records))),
    )


async def _today(monkeypatch, *records, latest=None):
    _patch_routines(monkeypatch, list(records))
    return await notify._routine_today(_Session(latest), _profile(), NOW)


# ── 대상·스트릭 계산 ─────────────────────────────────────────

async def test_streak_kept_until_yesterday(monkeypatch):
    assert await _today(monkeypatch, _record(done=range(1, 9))) == notify.RoutineToday(8, 0)


async def test_no_routines_or_nothing_today_or_done_or_skipped(monkeypatch):
    assert await _today(monkeypatch) is None
    not_wednesday = tuple(d for d in DAILY if d != TODAY.isoweekday())
    assert await _today(monkeypatch, _record(done=range(1, 9), days=not_wednesday)) is None
    assert await _today(monkeypatch, _record(done=range(0, 9))) is None
    assert await _today(monkeypatch, _record(done=range(1, 9), skips=[0])) is None
    assert await _today(monkeypatch, _record(done=range(1, 9), end=TODAY)) is None  # 오늘 삭제


async def test_one_routine_done_today_counts_as_done(monkeypatch):
    """루틴이 여럿이어도 오늘 하나라도 했으면 보내지 않는다."""
    assert await _today(monkeypatch, _record(done=range(1, 9)), _record(done=[0])) is None


async def test_broken_yesterday_reports_lost_length(monkeypatch):
    assert await _today(monkeypatch, _record(done=range(2, 10))) == notify.RoutineToday(0, 8)


async def test_day_after_the_break_day_is_plain(monkeypatch):
    assert await _today(monkeypatch, _record(done=range(3, 11))) == notify.RoutineToday(0, 0)


async def test_weekly_routine_keeps_streak_across_days_without_instances(monkeypatch):
    """주 1회(수요일) 루틴: 지난 두 수요일에 했으면 오늘 스트릭 2 — 사이 날은 예정이 없다."""
    record = _record(done=[7, 14], days=(TODAY.isoweekday(),))
    assert await _today(monkeypatch, record) == notify.RoutineToday(2, 0)


async def test_break_on_non_daily_routine_is_reported_on_next_routine_day(monkeypatch):
    """월·수·금 루틴에서 월요일을 빼먹으면 화요일엔 할 루틴이 없고, 수요일 아침에 깨진 길이를 알린다.
    그다음 루틴 날(금요일)부터는 요일 문구다."""
    mwf = (1, 3, 5)
    done = [5, 7, 9, 12, 14, 16]  # 지난주 월·수·금 + 그 전주 월·수·금(오늘 수요일 기준)
    record = _record(done=done, days=mwf, start=17)  # 시작일 = 그 전주 월요일
    assert await _today(monkeypatch, record) == notify.RoutineToday(0, 6)  # 월요일(2일 전) 안 함
    _patch_routines(monkeypatch, [record])
    later = await notify._routine_today(_Session(), _profile(), NOW + timedelta(days=2))  # 금요일
    assert later == notify.RoutineToday(0, 0)  # 수요일도 안 함 → 깨진 다음 루틴 날이 지났다


async def test_device_timezone_ahead_skips(monkeypatch):
    """기기 시간대로 저장된 완료가 서버 기준 오늘보다 뒤면 이미 한 것으로 보고 보내지 않는다."""
    assert await _today(monkeypatch, _record(done=range(1, 9)), latest=TODAY + timedelta(days=1)) is None
    assert await _today(monkeypatch, _record(done=range(1, 9)), latest=_d(1)) == notify.RoutineToday(8, 0)


# ── 09시 발송 ────────────────────────────────────────────────

@pytest.fixture
def morning(monkeypatch):
    calls = SimpleNamespace(
        push_on=AsyncMock(return_value=True), enabled=AsyncMock(return_value=True),
        today=AsyncMock(return_value=notify.RoutineToday(8, 0)),
        claim=AsyncMock(return_value=True), send=AsyncMock(return_value=1),
    )
    monkeypatch.setattr(notify, "_push_on", calls.push_on)
    monkeypatch.setattr(notify, "_enabled", calls.enabled)
    monkeypatch.setattr(notify, "_routine_today", calls.today)
    monkeypatch.setattr(notify, "_tokens", AsyncMock(return_value=["tok"]))
    monkeypatch.setattr(notify, "_claim_send_slot", calls.claim)
    monkeypatch.setattr(push, "prepare_access_token", AsyncMock(return_value="acc"))
    monkeypatch.setattr(push, "send", calls.send)
    return calls


async def test_weekday_copy_link_and_slot(morning):
    assert await notify.notify_routine_morning(_Session(), _profile(), now=NOW) == 1
    morning.send.assert_awaited_once_with(
        ["tok"], "한 주의 절반, 수요일이에요 💛", "오늘 루틴 미리 확인하고 하루를 시작해 볼까요?",
        data={"link": "routine"}, expires_at=NOW + timedelta(hours=2), access_token="acc",
        collapse_key="routine",
    )
    assert morning.claim.await_args.args[2] == "routine_notified_at"
    morning.push_on.assert_awaited_once_with(morning.push_on.await_args.args[0], "routine_morning")
    morning.enabled.assert_awaited_once()
    assert morning.enabled.await_args.args[2] == "evening_chat"


@pytest.mark.parametrize("lost,title", [
    (8, "아쉽게도 8일 스트릭이 깨졌어요 😢"),
    (1, "한 주의 절반, 수요일이에요 💛"),  # 1일짜리는 스트릭으로 보지 않는다
])
async def test_break_copy_only_for_two_days_or_more(morning, lost, title):
    morning.today.return_value = notify.RoutineToday(0, lost)
    assert await notify.notify_routine_morning(_Session(), _profile(), now=NOW) == 1
    assert morning.send.await_args.args[1] == title


@pytest.mark.parametrize("gate", ["hour", "switch_off", "chat_off", "not_target", "error"])
async def test_gates_do_not_claim_or_send(morning, gate):
    now = NOW
    if gate == "hour":
        now = NOW + timedelta(hours=1)  # KST 10:30
    if gate == "switch_off":
        morning.push_on.return_value = False
    if gate == "chat_off":
        morning.enabled.return_value = False
    if gate == "not_target":
        morning.today.return_value = None
    if gate == "error":
        morning.today.side_effect = RuntimeError("db")
    assert await notify.notify_routine_morning(_Session(), _profile(), now=now) == 0
    morning.claim.assert_not_awaited()
    morning.send.assert_not_awaited()


async def test_push_switch_defaults_off_and_reads_app_config(monkeypatch):
    async def _cfg(session, keys):
        return {"push_settings": {"routine_morning": True}}

    monkeypatch.setattr(notify, "get_config_values", _cfg)
    assert await notify._push_on(_Session(), "routine_morning") is True
    assert await notify._push_on(_Session(), "routine_risk") is False  # 없는 키 = 기본값(꺼짐)

    async def _boom(session, keys):
        raise RuntimeError("db")

    monkeypatch.setattr(notify, "get_config_values", _boom)
    assert await notify._push_on(_Session(), "routine_morning") is False


def test_expiry_is_two_hours_but_never_past_21():
    assert notify._expires_at(NOW, "Asia/Seoul") == NOW + timedelta(hours=2)
    evening = datetime(2026, 10, 14, 11, 45, tzinfo=timezone.utc)  # KST 20:45
    assert notify._expires_at(evening, "Asia/Seoul") == datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
