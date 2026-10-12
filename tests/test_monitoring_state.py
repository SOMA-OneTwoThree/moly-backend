"""지속 통지의 의미: 성공 ack, 경합, 미전송 사건, 재알림과 복구."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import monitoring_state as state

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def observation(active=True, fingerprint="a", **kw):
    return state.Observation("rc_production", active, fingerprint, "incident", **kw)


def notified(active=True, fingerprint="a", **kw):
    return dict(active=active, had_incident=True, last_notified_at=NOW.isoformat(),
                notified_active=active, notified_fingerprint=fingerprint,
                notified_severity="alert", **kw)


def test_first_failure_repeated_and_reminder():
    _, due = state.transition({}, observation(), NOW)
    assert due
    assert not state.transition(notified(), observation(), NOW + timedelta(minutes=15))[1]
    assert state.transition(notified(), observation(), NOW + timedelta(hours=6))[1]


def test_same_count_new_identity_has_cooldown():
    assert not state.transition(notified(), observation(fingerprint="new"), NOW + timedelta(minutes=15))[1]
    assert state.transition(notified(), observation(fingerprint="new"), NOW + timedelta(hours=1))[1]


def test_escalation_and_reopening_have_short_flap_floor():
    previous = notified()
    previous["notified_severity"] = "status"
    assert not state.transition(previous, observation(), NOW + timedelta(seconds=299))[1]
    assert state.transition(previous, observation(), NOW + timedelta(seconds=300))[1]
    assert not state.transition(notified(False), observation(), NOW + timedelta(seconds=299))[1]
    assert not state.transition(notified(), observation(importance=3), NOW + timedelta(seconds=299))[1]
    assert state.transition(notified(), observation(importance=3), NOW + timedelta(seconds=300))[1]


def test_unsent_reopened_incident_does_not_disappear_after_recovery():
    previous = notified(False, incident_number=1, notified_incident_number=1)
    reopened, due = state.transition(previous, observation(), NOW + timedelta(seconds=1))
    assert not due and reopened["incident_number"] == 2
    assert state.transition(reopened, observation(False), NOW + timedelta(seconds=2))[1]


def test_idle_unknown_and_old_observations_cannot_clear_incident():
    previous = notified(observed_at=NOW.isoformat())
    for obs, when in [(observation(None), NOW), (observation(False), NOW - timedelta(seconds=1))]:
        after, due = state.transition(previous, obs, when)
        assert after == previous and not due


def test_recovery_requires_incident_but_preserves_failed_delivery():
    assert not state.transition({}, observation(False), NOW)[1]
    assert state.transition({"active": True, "had_incident": True}, observation(False), NOW)[1]
    assert state.transition(notified(), observation(False), NOW)[1]
    assert not state.transition(notified(False), observation(False), NOW)[1]


def test_daily_date_is_value_and_failed_delivery_does_not_consume_day():
    obs = state.Observation("daily_status", True, "2026-10-12", "daily", daily=True, severity="status")
    assert state.transition({}, obs, NOW)[1]
    assert not state.transition({"last_notified_date": "2026-10-12"}, obs, NOW)[1]
    assert state.transition({"last_notified_date": "2026-10-11"}, obs, NOW)[1]


def test_claim_expiry_and_retry_delay():
    later = (NOW + timedelta(seconds=60)).isoformat()
    assert not state.transition({"lease_until": later}, observation(), NOW)[1]
    assert state.transition({"lease_until": later}, observation(), NOW + timedelta(seconds=60))[1]
    assert not state.transition({"retry_after": later}, observation(), NOW)[1]


class Session:
    def __init__(self, value):
        self.row = SimpleNamespace(value=value)
        self.writes = []

    async def execute(self, stmt):
        if stmt.is_select:
            return SimpleNamespace(scalar_one_or_none=lambda: self.row)
        self.writes.append(stmt.compile().params)
        return SimpleNamespace(rowcount=1)


@pytest.mark.parametrize("severity,delay", [("alert", 60), ("status", 300)])
async def test_failure_ack_releases_claim_without_success(severity, delay):
    session = Session({"lease_token": "lease", "lease_until": NOW.isoformat()})
    claim = state.Claim("key", "lease", True, "a", severity, False, "text")
    assert await state._ack(session, claim, sent=False, now=NOW)
    result = session.writes[0]["value"]
    assert "last_notified_at" not in result and "lease_token" not in result
    assert datetime.fromisoformat(result["retry_after"]) == NOW + timedelta(seconds=delay)


async def test_late_ack_cannot_touch_new_lease():
    session = Session({"lease_token": "new"})
    claim = state.Claim("key", "old", True, "a", "alert", False, "text")
    assert not await state._ack(session, claim, sent=True, now=NOW)
    assert session.writes == []


async def test_ack_preserves_new_observation_during_send():
    session = Session({"lease_token": "lease", "active": False, "fingerprint": "empty", "had_incident": True})
    claim = state.Claim("key", "lease", True, "a", "alert", False, "text")
    await state._ack(session, claim, sent=True, now=NOW)
    result = session.writes[0]["value"]
    assert result["active"] is False and result["fingerprint"] == "empty"
    assert result["notified_active"] is True
    assert state.transition(result, observation(False), NOW)[1]


async def test_db_failure_alert_fails_open_but_daily_status_does_not(monkeypatch):
    def unavailable():
        raise RuntimeError("private diagnostic")
    monkeypatch.setattr(state, "get_sessionmaker", unavailable)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(state.slack_notify, "send", send)
    assert await state.publish(observation(), NOW)
    assert "알림 상태 저장 불가" in send.call_args.args[0]
    assert not await state.publish(observation(severity="status", daily=True), NOW)
    assert send.await_count == 1


async def test_unknown_signal_and_unknown_observation_do_no_io():
    assert not await state.publish(observation(None), NOW)
    with pytest.raises(ValueError):
        await state.publish(state.Observation("dynamic-user-id", True, "a", "x"), NOW)
