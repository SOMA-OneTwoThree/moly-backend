"""One Apple original transaction can span several RevenueCat subscription objects.

Apple keeps original_transaction_id across a lapse/resubscribe or paid product change,
while RevenueCat starts a new subscription object. Lookups must follow the event's own
transaction, not the original one that only belongs to the oldest object.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.models.payment import Payment
from app.models.profile import Profile
from app.models.subscription import Subscription
from app.services import revenuecat, subscription
from tests.test_subscription import UID, UID_UUID, FakeSession, _rc_event, _Result

NOW = datetime.now(timezone.utc).replace(microsecond=0)
MONTHLY = "com.geniusjun.moly.plus.monthly"
YEARLY = "com.geniusjun.moly.plus.yearly"


def ms(value):
    return int(value.timestamp() * 1000)


def tx(identity, when, product, gross=8):
    return {"id": identity, "purchased_at": ms(when), "product_store_identifier": product,
            "revenue_in_local_currency": {"gross": gross}}


def rc_object(rc_id, latest, *, gives_access, ends):
    return {
        "id": rc_id, "customer_id": UID, "original_customer_id": UID,
        "store": "app_store", "environment": "sandbox", "store_subscription_identifier": latest,
        "gives_access": gives_access, "status": "active" if gives_access else "expired",
        "auto_renewal_status": "will_renew" if gives_access else "will_not_renew",
        "current_period_ends_at": ms(ends), "ends_at": ms(ends),
    }


OBJECTS = {
    "rc-yearly": rc_object("rc-yearly", "apple-original", gives_access=False, ends=NOW - timedelta(days=60)),
    "rc-monthly": rc_object("rc-monthly", "renewal-2", gives_access=True, ends=NOW + timedelta(days=30)),
}
TRANSACTIONS = {
    "rc-yearly": [tx("apple-original", NOW - timedelta(days=425), YEARLY)],
    "rc-monthly": [tx("paid-1", NOW - timedelta(days=30), MONTHLY), tx("renewal-2", NOW, MONTHLY)],
}


def provider(monkeypatch):
    async def search(store_tx):
        # RevenueCat matches any transaction of an object, past or current.
        return [OBJECTS[rc_id] for rc_id, txs in TRANSACTIONS.items() if store_tx in {t["id"] for t in txs}]

    async def get_subscription(rc_id):
        return OBJECTS[rc_id]

    async def transactions(rc_id):
        return TRANSACTIONS[rc_id]

    async def customer_subscriptions(uid):
        return list(OBJECTS.values())

    monkeypatch.setattr(revenuecat, "search_subscriptions", search)
    monkeypatch.setattr(revenuecat, "get_subscription", get_subscription)
    monkeypatch.setattr(revenuecat, "subscription_transactions", transactions)
    monkeypatch.setattr(revenuecat, "customer_subscriptions", customer_subscriptions)


def renewal():
    return _rc_event(type="RENEWAL", product_id=MONTHLY, original_transaction_id="apple-original",
                     transaction_id="renewal-2", environment="SANDBOX", price_in_purchased_currency=7.99,
                     currency="USD", purchased_at_ms=ms(NOW), expiration_at_ms=ms(NOW + timedelta(days=30)),
                     event_timestamp_ms=ms(NOW))


def row(rc_id, plan, status, original, latest, expires):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=UID_UUID, plan=plan, status=status, rc_subscription_id=rc_id,
        original_transaction_id=original, latest_transaction_id=latest, expires_at=expires,
        environment="SANDBOX", ownership_changed_at=None, last_event_at=NOW - timedelta(days=30),
        auto_renew_enabled=status == "active", store_trial_ends_at=None,
    )


class SplitChainSession(FakeSession):
    def __init__(self, rows):
        super().__init__(get_map={Profile: SimpleNamespace(created_at=NOW - timedelta(days=500))})
        self.rows = rows

    async def execute(self, stmt, params=None):
        if "pg_advisory_xact_lock" in str(stmt):
            return _Result([])
        if stmt.column_descriptions[0]["entity"] is Subscription:
            bound = set()
            for value in stmt.compile().params.values():
                bound.update(value if isinstance(value, list) else [value])
            return _Result([r for r in self.rows if {r.rc_subscription_id, r.original_transaction_id,
                                                     r.latest_transaction_id} & bound])
        return _Result([])


async def test_bonus_review_verifies_payment_in_object_split_from_original(monkeypatch):
    provider(monkeypatch)
    sub = row("rc-monthly", "monthly", "active", "paid-1", "paid-1", NOW)
    review = await subscription._bonus_review(SplitChainSession([sub]), sub, renewal(), "monthly")
    assert review == "not_granted:store_plan_previously_paid"


async def test_renewal_extends_current_object_not_expired_original(monkeypatch):
    provider(monkeypatch)
    monkeypatch.setattr(subscription.settings, "revenuecat_api_v2_key", "test-private-key")
    old_end, new_end = NOW - timedelta(days=60), NOW + timedelta(days=30)
    yearly = row("rc-yearly", "yearly", "expired", "apple-original", "apple-original", old_end)
    monthly = row("rc-monthly", "monthly", "active", "paid-1", "paid-1", NOW)
    yearly_before = vars(yearly).copy()
    session = SplitChainSession([yearly, monthly])
    result = await subscription.handle_revenuecat_event(session, renewal())

    assert result.outcome == subscription.HANDLED
    assert monthly.expires_at == new_end and monthly.latest_transaction_id == "renewal-2"
    assert vars(yearly) == yearly_before
    [pay] = [a for a in session.added if isinstance(a, Payment)]
    assert pay.subscription_id == monthly.id
    assert pay.subscription_bonus_review == "not_granted:store_plan_previously_paid"


@pytest.mark.parametrize("original_customer,recorded", [(UID, True), (str(uuid.uuid4()), False)])
async def test_resubscribe_object_new_to_us_records_payment_unless_transferred(
    monkeypatch, original_customer, recorded,
):
    provider(monkeypatch)
    monkeypatch.setitem(OBJECTS["rc-monthly"], "original_customer_id", original_customer)
    monkeypatch.setattr(subscription.settings, "revenuecat_api_v2_key", "test-private-key")
    yearly = row("rc-yearly", "yearly", "expired", "apple-original", "apple-original", NOW - timedelta(days=60))
    session = SplitChainSession([yearly])
    result = await subscription.handle_revenuecat_event(session, renewal())

    assert result.outcome == subscription.HANDLED
    [created] = [a for a in session.added if isinstance(a, Subscription)]
    assert created.rc_subscription_id == "rc-monthly" and created.status == "active"
    assert created.expires_at == NOW + timedelta(days=30)
    payments = [a for a in session.added if isinstance(a, Payment)]
    assert [p.subscription_id for p in payments] == ([created.id] if recorded else [])


def _grace(sub):
    return sub.status == "grace_period" and sub.expires_at == NOW + timedelta(days=3)


@pytest.mark.parametrize("overrides,changed", [
    ({"type": "BILLING_ISSUE", "grace_period_expiration_at_ms": ms(NOW + timedelta(days=3))}, _grace),
    ({"type": "CANCELLATION", "cancel_reason": "UNSUBSCRIBE"}, lambda sub: sub.auto_renew_enabled is False),
    ({"type": "EXPIRATION", "expiration_at_ms": ms(NOW)}, lambda sub: sub.status == "expired"),
    ({"type": "CANCELLATION", "cancel_reason": "CUSTOMER_SUPPORT", "period_type": "TRIAL",
      "price_in_purchased_currency": 0}, lambda sub: sub.status == "revoked"),
    ({"type": "CANCELLATION", "cancel_reason": "CUSTOMER_SUPPORT"},
     lambda sub: sub.expires_at == NOW + timedelta(days=30)),  # no Payment: provider access synced
])
async def test_status_events_apply_to_current_object_row(monkeypatch, overrides, changed):
    provider(monkeypatch)
    yearly = row("rc-yearly", "yearly", "expired", "apple-original", "apple-original", NOW - timedelta(days=60))
    monthly = row("rc-monthly", "monthly", "active", "paid-1", "renewal-2", NOW)
    yearly_before = vars(yearly).copy()
    event = _rc_event(original_transaction_id="apple-original", transaction_id="renewal-2",
                      environment="SANDBOX", expiration_at_ms=ms(NOW), event_timestamp_ms=ms(NOW))
    event.update(overrides)
    await subscription.handle_revenuecat_event(SplitChainSession([yearly, monthly]), event)

    assert changed(monthly)
    assert vars(yearly) == yearly_before
