from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import subscription, subscription_periods as periods

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def ms(value):
    return int(value.timestamp()*1000)


def sub(**kwargs):
    return SimpleNamespace(purchased_at=None, expires_at=NOW+timedelta(days=3),
                           ownership_changed_at=None, latest_transaction_id="latest", **kwargs)


def session_with(*items):
    return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: list(items)))))


@pytest.mark.parametrize("value", [None, True, "invalid", float("nan"), float("inf")])
def test_period_time_requires_a_store_timestamp(value):
    assert periods.period_time(value) is None


async def test_period_event_accepts_paid_or_trial_with_nullable_row_start(monkeypatch):
    row = sub()
    events = [{"purchased_at_ms": ms(NOW-timedelta(days=1)), "expiration_at_ms": ms(NOW+timedelta(days=1))}]
    monkeypatch.setattr(periods, "period_events", AsyncMock(return_value=events))
    assert await periods.subscription_at(session_with(row), "test", NOW) is row


async def test_period_without_evidence_stays_retryable(monkeypatch):
    monkeypatch.setattr(periods, "period_events", AsyncMock(return_value=[]))
    with pytest.raises(ValueError, match="subscription period unavailable"):
        await periods.subscription_at(session_with(sub()), "test", NOW)


async def test_verified_later_purchase_does_not_cover_previous_day(monkeypatch):
    event = dict(transaction_id="latest", purchased_at_ms=ms(NOW+timedelta(days=1)),
                 expiration_at_ms=ms(NOW+timedelta(days=2)))
    monkeypatch.setattr(periods, "period_events", AsyncMock(return_value=[event]))
    assert await periods.subscription_at(session_with(sub()), "test", NOW) is None


async def test_verified_current_start_covers_extended_expiry(monkeypatch):
    event = dict(transaction_id="latest", purchased_at_ms=ms(NOW-timedelta(days=3)),
                 expiration_at_ms=ms(NOW-timedelta(days=1)))
    monkeypatch.setattr(periods, "period_events", AsyncMock(return_value=[event]))
    assert await periods.subscription_at(session_with(sub()), "test", NOW)


async def test_renewal_allows_a_prior_period_without_bridging_its_gap(monkeypatch):
    row = sub()
    row.purchased_at = NOW+timedelta(days=1)
    events = [{"purchased_at_ms": ms(NOW-timedelta(days=3)), "expiration_at_ms": ms(NOW-timedelta(days=1))}]
    monkeypatch.setattr(periods, "period_events", AsyncMock(return_value=events))
    assert await periods.subscription_at(session_with(row), "test", NOW-timedelta(days=2)) is row
    assert await periods.subscription_at(session_with(row), "test", NOW) is None


def test_transfer_period_is_bounded_by_current_ownership():
    event = {"purchased_at_ms": ms(NOW-timedelta(days=2)), "expiration_at_ms": ms(NOW+timedelta(days=2))}
    assert not periods.contains(event, NOW-timedelta(hours=1), NOW)
    assert periods.contains(event, NOW, NOW)


@pytest.mark.parametrize("kind", ["INITIAL_PURCHASE", "RENEWAL"])
def test_active_event_keeps_start_and_expiry_in_the_same_period(kind):
    row = SimpleNamespace(status="active", expires_at=NOW, last_event_at=None, purchased_at=None)
    subscription._apply_active_state(row, kind, "yearly", NOW+timedelta(days=30), "test", NOW,
                                     purchased_at=NOW-timedelta(hours=1))
    assert row.purchased_at == NOW-timedelta(hours=1)


def test_earlier_state_does_not_replace_current_period_start():
    row = SimpleNamespace(status="active", expires_at=NOW+timedelta(days=5), last_event_at=NOW,
                          purchased_at=NOW-timedelta(hours=1))
    subscription._apply_active_state(row, "INITIAL_PURCHASE", "yearly", NOW, "old",
                                     NOW-timedelta(days=5), purchased_at=NOW-timedelta(days=6))
    assert row.purchased_at == NOW-timedelta(hours=1)


def test_same_period_notification_keeps_verified_start():
    row = SimpleNamespace(status="active", expires_at=NOW+timedelta(days=5), last_event_at=None,
                          latest_transaction_id="same", purchased_at=NOW-timedelta(hours=1))
    subscription._apply_active_state(row, "RENEWAL", "yearly", row.expires_at, "same", NOW)
    assert row.purchased_at == NOW-timedelta(hours=1)
