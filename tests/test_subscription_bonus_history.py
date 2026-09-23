"""Store-chain bonus proof and immutable financial recipient regressions."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.hay_transaction import HayTransaction
from app.models.profile import Profile
from app.models.subscription import Subscription
from app.services import revenuecat, subscription
from tests.test_subscription import FakeSession, SUB_ID, UID_UUID, _rc_event, _sub

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
WHEN = int(NOW.timestamp() * 1000)
PID = "com.geniusjun.moly.plus.monthly"


@pytest.fixture(autouse=True)
def no_other_provider_objects(monkeypatch):
    monkeypatch.setattr(revenuecat, "customer_subscriptions", AsyncMock(return_value=[]))


def tx(identity, when=WHEN, gross=8, product=PID):
    return {"id": identity, "purchased_at": when, "product_store_identifier": product,
            "revenue_in_local_currency": {"gross": gross}}


@pytest.mark.parametrize("history,proof,expected", [
    ([], [], "eligible"),
    ([tx("prior", WHEN - 1)], [], "not_granted:store_plan_previously_paid"),
    ([tx("prior", WHEN - 1, product="com.geniusjun.moly.plus.yearly")], [], "eligible"),
    ([tx("prior", WHEN - 1, gross=0)], [], "held:historical_zero_or_refund_unknown"),
    ([tx("prior", WHEN - 1, gross=0)], [_rc_event(period_type="NORMAL", price_in_purchased_currency=0)], "eligible"),
    ([tx("prior", WHEN - 1, gross=0)], [_rc_event(price_in_purchased_currency=6900)], "not_granted:store_plan_previously_paid"),
    ([tx("prior", WHEN - 1, gross=None)], [_rc_event(period_type="TRIAL")], "held:historical_zero_or_refund_unknown"),
])
async def test_same_store_plan_history_controls_bonus(monkeypatch, history, proof, expected):
    monkeypatch.setattr(subscription, "_snapshot_for_transaction", AsyncMock(return_value=(
        {"id": "rc-current", "customer_id": str(UID_UUID), "original_customer_id": str(UID_UUID), "store": "play_store", "environment": "sandbox"}, [*history, tx("t-rc-1")],
    )))
    profile = SimpleNamespace(created_at=datetime(2029, 1, 1, tzinfo=timezone.utc))
    session = FakeSession(exec_results=[[SimpleNamespace(payload=e) for e in proof]], get_map={Profile: profile})
    assert await subscription._bonus_review(session, _sub(environment="SANDBOX"), _rc_event(), "monthly") == expected
    assert session.added == []


async def test_recreated_account_cannot_claim_historical_first_paid_bonus(monkeypatch):
    monkeypatch.setattr(subscription, "_snapshot_for_transaction", AsyncMock(return_value=(
        {"id": "rc-current", "customer_id": str(UID_UUID), "original_customer_id": str(UID_UUID), "store": "play_store", "environment": "sandbox"}, [tx("t-rc-1")],
    )))
    session = FakeSession(get_map={Profile: SimpleNamespace(created_at=datetime(2031, 1, 1, tzinfo=timezone.utc))})
    assert await subscription._bonus_review(session, _sub(environment="SANDBOX"), _rc_event(), "monthly") == "not_granted:historical_purchase"


async def test_unavailable_provider_history_holds_bonus(monkeypatch):
    monkeypatch.setattr(subscription, "_snapshot_for_transaction", AsyncMock(side_effect=revenuecat.RevenueCatError("unavailable")))
    assert await subscription._bonus_review(FakeSession(), _sub(environment="SANDBOX"), _rc_event(), "monthly") == "held:provider_history_unavailable"


async def test_transferred_subscription_refund_targets_only_actual_original_bonus(monkeypatch):
    new_owner = uuid.uuid4()
    grant_id = uuid.uuid4()
    pay = SimpleNamespace(user_id=UID_UUID, subscription_id=SUB_ID, store_transaction_id="original-paid",
                          subscription_plan="monthly", subscription_hay_grant_id=grant_id,
                          subscription_bonus_review="granted", status="paid")
    sub = _sub(user_id=new_owner, plan="yearly", latest_transaction_id="new-renewal")
    grant = SimpleNamespace(id=grant_id, hay_transaction_id=99, revoked_at=None, clawback_hay_transaction_id=None)
    ledger = AsyncMock(return_value=SimpleNamespace(id=100))
    monkeypatch.setattr(subscription.hay_ledger, "apply", ledger)
    session = FakeSession(exec_results=[[grant]], get_map={Subscription: sub, HayTransaction: SimpleNamespace(amount=1000)})
    result = await subscription._refund_subscription(session, pay, NOW)
    assert result.outcome == subscription.HANDLED
    ledger.assert_awaited_once_with(session, UID_UUID, "refund_revoke", -1000, allow_negative=True)
    assert sub.user_id == new_owner and sub.status == "active" and sub.plan == "yearly"
    assert pay.status == "refunded"


@pytest.mark.parametrize("review", [None, "not_granted:store_plan_previously_paid", "held:provider_history_unavailable"])
async def test_unlinked_bonus_is_never_inferred_from_current_plan(monkeypatch, review):
    pay = SimpleNamespace(user_id=UID_UUID, subscription_id=SUB_ID, store_transaction_id="current-paid",
                          subscription_plan=None, subscription_hay_grant_id=None,
                          subscription_bonus_review=review, status="paid")
    sub = _sub(plan="yearly", latest_transaction_id="current-paid")
    ledger = AsyncMock()
    monkeypatch.setattr(subscription.hay_ledger, "apply", ledger)
    session = FakeSession(get_map={Subscription: sub})
    result = await subscription._refund_subscription(session, pay, NOW)
    assert result.outcome == subscription.HANDLED
    assert pay.status == "refunded" and sub.status == "revoked"
    ledger.assert_not_awaited()


async def test_legacy_reverse_is_idempotent_without_inventing_bonus_link(monkeypatch):
    pay = SimpleNamespace(user_id=UID_UUID, subscription_id=SUB_ID, store_transaction_id="current-paid",
                          subscription_bonus_review=None, status="refunded")
    sub = _sub(status="revoked", latest_transaction_id="current-paid")
    ledger = AsyncMock()
    monkeypatch.setattr(subscription.hay_ledger, "apply", ledger)
    session = FakeSession(get_map={Subscription: sub})
    assert (await subscription._reverse_subscription_refund(session, pay, NOW)).outcome == subscription.HANDLED
    assert (await subscription._reverse_subscription_refund(session, pay, NOW)).outcome == subscription.NO_OP
    assert pay.status == "paid"
    ledger.assert_not_awaited()


@pytest.mark.parametrize("gross", [0, -1, None, "NaN", "Infinity"])
async def test_current_provider_amount_must_confirm_paid_before_bonus(monkeypatch, gross):
    monkeypatch.setattr(subscription, "_snapshot_for_transaction", AsyncMock(return_value=(
        {"customer_id": str(UID_UUID)}, [tx("t-rc-1", gross=gross)],
    )))
    result = await subscription._bonus_review(FakeSession(), _sub(environment="SANDBOX"), _rc_event(), "monthly")
    assert result == "held:current_payment_amount_unverified"


async def test_product_change_split_into_new_rc_object_does_not_regrant(monkeypatch):
    snapshot = {"id": "new-object", "customer_id": str(UID_UUID), "original_customer_id": str(UID_UUID),
                "store": "play_store", "environment": "sandbox"}
    monkeypatch.setattr(subscription, "_snapshot_for_transaction", AsyncMock(return_value=(snapshot, [tx("t-rc-1")])))
    monkeypatch.setattr(revenuecat, "customer_subscriptions", AsyncMock(return_value=[snapshot, {
        "id": "old-object", "store": "play_store", "environment": "sandbox",
    }]))
    monkeypatch.setattr(revenuecat, "subscription_transactions", AsyncMock(return_value=[tx("old-paid", WHEN - 1)]))
    session = FakeSession(get_map={Profile: SimpleNamespace(created_at=datetime(2029, 1, 1, tzinfo=timezone.utc))})
    assert await subscription._bonus_review(session, _sub(environment="SANDBOX"), _rc_event(), "monthly") == "held:related_store_subscription_history"


async def test_new_plan_after_transfer_is_not_held_only_for_different_original_owner(monkeypatch):
    snapshot = {"id": "new-object", "customer_id": str(UID_UUID), "original_customer_id": str(uuid.uuid4()),
                "store": "play_store", "environment": "sandbox"}
    monkeypatch.setattr(subscription, "_snapshot_for_transaction", AsyncMock(return_value=(snapshot, [tx("t-rc-1")])))
    session = FakeSession(get_map={Profile: SimpleNamespace(created_at=datetime(2029, 1, 1, tzinfo=timezone.utc))})
    assert await subscription._bonus_review(session, _sub(environment="SANDBOX"), _rc_event(), "monthly") == "eligible"
