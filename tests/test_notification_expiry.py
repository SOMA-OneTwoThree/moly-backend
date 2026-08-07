"""알림 창(expiry) — 놓친 알림을 뒤늦게 쏘지 않는다.

09:00·20:00 푸시는 **그 시각의 창에서만** 나간다. 워커가 멈췄다 복구해도 지나간 알림은
버려져야 한다 — 밤 11시에 받는 '좋은 아침'은 알림이 아니라 사고다.

기존 test_notify.py는 `_claim_send_slot`을 mock해서 claim의 *소비자*만 본다. 여기서는
**어느 시각에 발송 경로가 열리는지**를 본다(창 자체의 검증).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from worker import tick
from tests.test_worker_tick import _fake_get_sessionmaker


@pytest.fixture
def seoul_user(monkeypatch):
    p = SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Seoul")

    async def _cfg(session):
        return {}

    monkeypatch.setattr(tick, "get_sessionmaker", _fake_get_sessionmaker([p]))
    monkeypatch.setattr(tick, "effective_token_config", _cfg)
    return p


def _kst(hour: int, minute: int = 0) -> datetime:
    """KST 기준 시각을 UTC로. KST = UTC+9."""
    utc_hour = (hour - 9) % 24
    day = 6 if hour >= 9 else 7  # 자정을 넘기면 다음 UTC 날짜
    return datetime(2026, 7, day, utc_hour, minute, tzinfo=timezone.utc)


async def _sent(monkeypatch, when: datetime) -> dict:
    calls = {"morning": 0, "evening": 0}

    async def _m(session, p, now=None):
        calls["morning"] += 1
        return 1

    async def _e(session, p, now=None, stats=None):
        calls["evening"] += 1
        return 1

    monkeypatch.setattr(tick.notify, "notify_morning", _m)
    monkeypatch.setattr(tick.notify, "notify_evening", _e)
    await tick.run_tick(when)
    return calls


async def test_morning_fires_inside_its_hour(monkeypatch, seoul_user):
    assert (await _sent(monkeypatch, _kst(9, 0)))["morning"] == 1
    assert (await _sent(monkeypatch, _kst(9, 45)))["morning"] == 1


@pytest.mark.parametrize("hour", [10, 11, 14, 23])
async def test_missed_morning_is_dropped_not_deferred(monkeypatch, seoul_user, hour):
    """창을 놓치면 그날은 버린다. 밀린 알림이 뒤늦게 나가면 안 된다."""
    assert (await _sent(monkeypatch, _kst(hour)))["morning"] == 0


async def test_evening_fires_inside_its_hour(monkeypatch, seoul_user):
    assert (await _sent(monkeypatch, _kst(20, 30)))["evening"] == 1


@pytest.mark.parametrize("hour", [19, 21, 22])
async def test_missed_evening_is_dropped(monkeypatch, seoul_user, hour):
    assert (await _sent(monkeypatch, _kst(hour)))["evening"] == 0


async def test_the_two_pushes_never_share_an_hour(monkeypatch, seoul_user):
    """같은 틱에서 둘 다 나가면 유저는 두 번 알림을 받는다."""
    assert tick.MORNING_HOUR != tick.EVENING_HOUR
    for h in (tick.MORNING_HOUR, tick.EVENING_HOUR):
        c = await _sent(monkeypatch, _kst(h))
        assert c["morning"] + c["evening"] == 1


def test_dedup_fixture_matches_the_real_claim_columns():
    """fixture가 실제 claim과 다른 컬럼을 재면 통과해도 의미가 없다.

    fixture는 SQL을 복제하므로 drift가 유일한 위험이다. 최소한 같은 컬럼과 같은
    `IS NULL` 가드를 쓰는지는 고정한다.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "scripts" / "verify_notification_fixture.py"
    ).read_text()
    assert "morning_notified_at" in src and "evening_notified_at" in src
    assert "IS NULL" in src, "조건부 upsert 가드가 빠지면 항상 통과한다"
    assert "user_daily_stats" in src
