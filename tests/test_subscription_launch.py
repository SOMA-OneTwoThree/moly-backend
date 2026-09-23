"""Subscription launch regressions: free periods, paid conversion and grace access."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.payment import Payment
from app.models.subscription import Subscription
from app.services import subscription
from app.services.entitlement import derive_entitlement
from tests.test_subscription import FakeSession, UID, UID_UUID, _rc_event, _sub


@pytest.mark.parametrize("plan", ["monthly", "yearly"])
@pytest.mark.parametrize(
    "event_type,period,amount",
    [
        ("INITIAL_PURCHASE", "TRIAL", 0),
        ("INITIAL_PURCHASE", "TRIAL", 5900),
        ("RENEWAL", "NORMAL", 0),
    ],
)
async def test_free_period_grants_access_without_payment_or_hay(
    monkeypatch, plan, event_type, period, amount
):
    monkeypatch.setattr(subscription, "_by_original_tx", AsyncMock(return_value=None))
    grant = AsyncMock()
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    session = FakeSession()
    result = await subscription.handle_revenuecat_event(
        session,
        _rc_event(
            type=event_type,
            product_id=f"com.geniusjun.moly.plus.{plan}",
            period_type=period,
            price_in_purchased_currency=amount,
            currency="KRW",
        ),
    )
    assert result.outcome == subscription.HANDLED
    assert any(isinstance(row, Subscription) and row.status == "active" for row in session.added)
    assert not any(isinstance(row, Payment) for row in session.added)
    grant.assert_not_awaited()


@pytest.mark.parametrize("amount", [None, "NaN", "Infinity", -1])
async def test_unverified_payment_amount_never_grants_hay(monkeypatch, amount):
    monkeypatch.setattr(subscription, "_by_original_tx", AsyncMock(return_value=None))
    grant = AsyncMock()
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    session = FakeSession()
    result = await subscription.handle_revenuecat_event(
        session, _rc_event(period_type="NORMAL", price_in_purchased_currency=amount)
    )
    assert result.outcome == subscription.HANDLED
    assert result.reason  # durable inbox reason; a missing amount is not a free purchase.
    assert not any(isinstance(row, Payment) for row in session.added)
    grant.assert_not_awaited()


@pytest.mark.parametrize("period", ["NORMAL", "INTRO", "PROMOTIONAL"])
async def test_paid_trial_conversion_records_payment_and_clears_trial(monkeypatch, period):
    monkeypatch.setattr(subscription, "_bonus_review", AsyncMock(return_value="eligible"))
    sub = _sub(store_trial_ends_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(subscription, "_by_original_tx", AsyncMock(return_value=sub))
    grant = AsyncMock()
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    session = FakeSession()
    result = await subscription.handle_revenuecat_event(
        session,
        _rc_event(
            type="RENEWAL",
            period_type=period,
            is_trial_conversion=True,
            price_in_purchased_currency=6900,
            currency="KRW",
        ),
    )
    assert result.outcome == subscription.HANDLED
    assert sub.store_trial_ends_at is None
    grant.assert_awaited_once_with(session, UID_UUID, "monthly")
    assert len([row for row in session.added if isinstance(row, Payment)]) == 1


async def test_trial_is_retained_on_cancellation_and_reported(monkeypatch):
    ends = datetime.now(timezone.utc) + timedelta(days=20)
    sub = _sub(expires_at=ends, store_trial_ends_at=ends)
    monkeypatch.setattr(subscription, "_by_original_tx", AsyncMock(return_value=sub))
    await subscription.handle_revenuecat_event(
        FakeSession(),
        _rc_event(
            type="CANCELLATION",
            cancel_reason="UNSUBSCRIBE",
            period_type="TRIAL",
            price_in_purchased_currency=0,
        ),
    )
    assert sub.auto_renew_enabled is False
    monkeypatch.setattr(
        subscription,
        "_load_profile",
        AsyncMock(return_value=SimpleNamespace(id=UID_UUID, trial_ends_at=None)),
    )
    monkeypatch.setattr(subscription, "_latest_sub", AsyncMock(return_value=sub))
    monkeypatch.setattr(subscription, "effective_token_config", AsyncMock(return_value={}))
    response = await subscription.get_subscription(FakeSession(), UID)
    assert response["in_trial"] is True
    assert response["trial_ends_at"] == ends.isoformat()


async def test_grace_uses_revenuecat_entitlement_expiry(monkeypatch):
    sub = _sub(expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(subscription, "_by_original_tx", AsyncMock(return_value=sub))
    grace_ms = 1_900_000_000_000
    result = await subscription.handle_revenuecat_event(
        FakeSession(), _rc_event(type="BILLING_ISSUE", grace_period_expiration_at_ms=grace_ms)
    )
    assert result.outcome == subscription.HANDLED
    assert sub.status == "grace_period"
    assert sub.expires_at == datetime.fromtimestamp(grace_ms / 1000, timezone.utc)


async def test_free_trial_refund_without_payment_revokes_access_idempotently(monkeypatch):
    sub = _sub(latest_transaction_id="t-rc-1")
    monkeypatch.setattr(subscription, "_by_original_tx", AsyncMock(return_value=sub))
    clawback = AsyncMock()
    monkeypatch.setattr(subscription, "_clawback_subscription_grant", clawback)
    event = _rc_event(
        type="CANCELLATION",
        cancel_reason="CUSTOMER_SUPPORT",
        period_type="TRIAL",
        price_in_purchased_currency=0,
    )
    first = await subscription.handle_revenuecat_event(FakeSession(), event)
    second = await subscription.handle_revenuecat_event(FakeSession(), event)
    assert first.outcome == subscription.HANDLED
    assert second.outcome == subscription.NO_OP
    assert sub.status == "revoked"
    clawback.assert_not_awaited()


async def test_old_trial_refund_does_not_revoke_paid_renewal(monkeypatch):
    sub = _sub(latest_transaction_id="paid-renewal")
    monkeypatch.setattr(subscription, "_by_original_tx", AsyncMock(return_value=sub))
    result = await subscription.handle_revenuecat_event(
        FakeSession(),
        _rc_event(
            type="CANCELLATION",
            cancel_reason="CUSTOMER_SUPPORT",
            period_type="TRIAL",
            price_in_purchased_currency=0,
        ),
    )
    assert result.outcome == subscription.NO_OP
    assert sub.status == "active"


def test_launch_rollout_uses_only_explicit_app_trial_and_unlocks_all_benefits():
    now = datetime.now(timezone.utc)
    profile = SimpleNamespace(
        app_trial_started_at=now,
        trial_ends_at=now + timedelta(days=500),
        app_trial_ends_at=now + timedelta(hours=48),
    )
    cfg = {
        "subscription_launch": {"enabled": True},
        "free_launch_until": (now + timedelta(days=100)).isoformat(),
        "daily_token_limit": {"free": 20, "trial": 100, "subscriber": 100},
    }
    trial = derive_entitlement(profile, None, 0, cfg, now)
    assert trial["trial_ends_at"] == profile.app_trial_ends_at
    assert trial["subscriber_theme_unlocked"] is True
    assert trial["is_subscriber"] is False
    assert trial["ads_removed"] is True
    profile.app_trial_ends_at = now - timedelta(seconds=1)
    free = derive_entitlement(profile, None, 0, cfg, now)
    assert free["plan"] == "free" and free["ads_removed"] is False
    assert free["daily_token_limit"] == 20


def test_store_trial_keeps_subscriber_entitlement_and_trial_expiry():
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=30)
    entitlement = derive_entitlement(
        SimpleNamespace(trial_ends_at=None), _sub(store_trial_ends_at=end), 0, {}, now
    )
    assert entitlement["is_subscriber"] is True
    assert entitlement["trial_ends_at"] == end
    assert entitlement["ads_removed"] is True


async def test_sandbox_receipts_preserve_existing_review_and_testflight_policy(monkeypatch):
    active = AsyncMock(return_value=subscription.HandlerResult(subscription.HANDLED))
    monkeypatch.setattr(subscription, "_handle_active", active)
    result = await subscription.handle_revenuecat_event(
        FakeSession(), _rc_event(environment="SANDBOX")
    )
    assert result.outcome == subscription.HANDLED
    active.assert_awaited_once()


@pytest.mark.parametrize("live_trial", [True, False])
async def test_app_trial_allows_subscriber_items_only_until_expiry(monkeypatch, live_trial):
    from app.services import shop

    now = datetime.now(timezone.utc)
    profile = SimpleNamespace(
        app_trial_started_at=now,
        trial_ends_at=now + timedelta(days=100),
        app_trial_ends_at=now + timedelta(hours=1 if live_trial else -1),
    )
    from app.models.profile import Profile

    monkeypatch.setattr(shop, "_load_active_subscription", AsyncMock(return_value=None))
    assert await shop._subscribed(FakeSession(get_map={Profile: profile}), UID) is live_trial


async def test_free_period_refund_reversal_restores_without_bonus(monkeypatch):
    sub = _sub(status="revoked", latest_transaction_id="t-rc-1")
    monkeypatch.setattr(subscription, "_by_original_tx", AsyncMock(return_value=sub))
    grant = AsyncMock()
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    result = await subscription.handle_revenuecat_event(
        FakeSession(),
        _rc_event(type="REFUND_REVERSED", period_type="TRIAL", price_in_purchased_currency=0),
    )
    assert result.outcome == subscription.HANDLED
    assert sub.status == "active"
    grant.assert_not_awaited()


def test_app_trial_requires_recorded_start():
    now = datetime.now(timezone.utc)
    profile = SimpleNamespace(
        trial_ends_at=None, app_trial_started_at=None, app_trial_ends_at=now + timedelta(hours=48)
    )
    result = derive_entitlement(profile, None, 0, {"subscription_launch": {"enabled": True}}, now)
    assert result["plan"] == "free"


def test_late_trial_event_cannot_restore_trial_after_paid_conversion():
    sub = _sub(
        expires_at=datetime.fromtimestamp(1800000000, timezone.utc),
        last_event_at=datetime.fromtimestamp(1800000000, timezone.utc),
        store_trial_ends_at=None,
    )
    subscription._apply_active_state(
        sub,
        "RENEWAL",
        "monthly",
        datetime.fromtimestamp(1900000000, timezone.utc),
        "old-trial",
        datetime.fromtimestamp(1700000000, timezone.utc),
        "TRIAL",
    )
    assert sub.store_trial_ends_at is None


def test_unmigrated_account_keeps_legacy_launch_after_rollout_enabled():
    now = datetime.now(timezone.utc)
    old_end = now + timedelta(days=10)
    profile = SimpleNamespace(trial_ends_at=None, app_trial_started_at=None, app_trial_ends_at=None)
    result = derive_entitlement(
        profile,
        None,
        0,
        {
            "subscription_launch": {"enabled": True},
            "free_launch_until": old_end.isoformat(),
            "free_launch_token_limit": 150000,
        },
        now,
    )
    assert result["plan"] == "trial" and result["trial_ends_at"] == old_end
    assert result["daily_token_limit"] == 150000
    assert result["subscriber_theme_unlocked"] is False


def test_prepared_rollout_keeps_former_test_accounts_in_launch_without_reissuing_trial():
    now = datetime.now(timezone.utc)
    profile = SimpleNamespace(
        trial_ends_at=now + timedelta(days=10),
        app_trial_started_at=now - timedelta(days=3),
        app_trial_ends_at=now - timedelta(days=1),
    )
    result = derive_entitlement(
        profile,
        None,
        0,
        {
            "subscription_launch": {"enabled": False},
            "free_launch_until": (now + timedelta(days=10)).isoformat(),
        },
        now,
    )
    assert result["plan"] == "trial" and result["entitlement_source"] == "launch"
    assert profile.app_trial_ends_at == now - timedelta(days=1)


async def test_store_trial_marks_existing_claim_even_if_enrollment_is_paused():
    session = AsyncMock()
    await subscription._mark_store_trial_offer_redeemed(session, UID_UUID, {
        "offer_code": "legacy-month", "product_id": "monthly", "store": "APP_STORE",
    })
    sql = session.execute.await_args.args[0]
    assert "UPDATE public.subscription_offer_claims" in str(sql)
    assert "redeemed_at IS NULL" in str(sql)
    assert sql.compile().params == {"uid": UID_UUID, "offer": "legacy-month",
                                    "product": "monthly", "store": "APP_STORE"}


@pytest.mark.parametrize("period", ["TRIAL", "INTRO", "NORMAL"])
async def test_zero_price_store_period_consumes_offer_and_reports_trial(monkeypatch, period):
    monkeypatch.setattr(subscription, "_by_original_tx", AsyncMock(return_value=None))

    class CaptureSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.statements = []

        async def execute(self, statement, params=None):
            self.statements.append(str(statement))
            return await super().execute(statement, params)

    session = CaptureSession()
    result = await subscription.handle_revenuecat_event(
        session, _rc_event(period_type=period, price_in_purchased_currency=0,
                           offer_code="legacy-month", store="APP_STORE")
    )
    assert result.outcome == subscription.HANDLED
    sub = next(row for row in session.added if isinstance(row, Subscription))
    assert sub.store_trial_ends_at == sub.expires_at
    assert sum("UPDATE public.subscription_offer_claims" in sql for sql in session.statements) == 1
    assert not any(isinstance(row, Payment) for row in session.added)


@pytest.mark.parametrize("active_status", ["active", "grace_period"])
async def test_current_monthly_wins_over_revoked_future_yearly(active_status):
    import uuid
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite://")
    Subscription.__table__.create(engine)
    now = datetime.now(timezone.utc)
    uid = uuid.UUID("abcdefab-cdef-abcd-efab-cdefabcdefab")
    with Session(engine) as session:
        for plan, status, days in [("yearly", "revoked", 365), ("monthly", active_status, 30)]:
            session.add(
                Subscription(
                    id=uuid.uuid4(),
                    user_id=uid,
                    plan=plan,
                    status=status,
                    original_transaction_id=plan,
                    expires_at=now + timedelta(days=days),
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
        async_session = SimpleNamespace(execute=AsyncMock(side_effect=session.execute))
        result = await subscription._latest_sub(async_session, uid)
        assert result.plan == "monthly"
        assert result.status == active_status
    engine.dispose()


@pytest.mark.parametrize("plan", ["monthly", "yearly"])
@pytest.mark.parametrize("store_trial", [False, True])
async def test_ads_return_when_paid_or_store_trial_subscription_expires(plan, store_trial):
    import uuid
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.services.account import _load_active_subscription

    engine = create_engine("sqlite://")
    Subscription.__table__.create(engine)
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    uid = uuid.UUID("abcdefab-cdef-abcd-efab-cdefabcdefab")
    with Session(engine) as session:
        session.add(
            Subscription(
                id=uuid.uuid4(),
                user_id=uid,
                plan=plan,
                status="active",
                original_transaction_id="ad-expiry",
                expires_at=end,
                store_trial_ends_at=end if store_trial else None,
                created_at=end - timedelta(days=30),
                updated_at=end,
            )
        )
        session.commit()
        async_session = SimpleNamespace(execute=AsyncMock(side_effect=session.execute))
        for offset_us in [-1, 0, 1]:
            now = end + timedelta(microseconds=offset_us)
            active = await _load_active_subscription(async_session, str(uid), now)
            # SQLite strips timezone from selected datetimes; PostgreSQL preserves it.
            if active is not None and active.store_trial_ends_at is not None:
                active.store_trial_ends_at = active.store_trial_ends_at.replace(tzinfo=timezone.utc)
            result = derive_entitlement(SimpleNamespace(trial_ends_at=None), active, 0, {}, now)
            assert result["plan"] == (plan if offset_us < 0 else "free")
            assert result["ads_removed"] is (offset_us < 0)
    engine.dispose()


@pytest.mark.parametrize("event", [
    {}, {"offer_code": None, "product_id": "monthly", "store": "APP_STORE"},
    {"offer_code": "legacy", "product_id": "monthly", "store": "STRIPE"},
])
async def test_unattributed_free_period_never_consumes_campaign(event):
    session = AsyncMock()
    await subscription._mark_store_trial_offer_redeemed(session, UID_UUID, event)
    session.execute.assert_not_awaited()
