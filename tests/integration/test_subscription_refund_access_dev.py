"""Real subscription transactions on the explicitly selected existing dev database.

Only random synthetic accounts are read. Provider responses are synthetic. An outer
transaction survives process_event's savepoint commits and rolls back all fixture
rows, including money, hay, inbox stages and the two local rollout configuration keys.
PostgreSQL sequence values may advance, as with the existing rollback DB tests.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.hay_transaction import HayTransaction
from app.models.payment import Payment
from app.models.profile import Profile
from app.models.revenuecat_event import RevenuecatEvent
from app.models.subscription import Subscription
from app.models.subscription_hay_grant import SubscriptionHayGrant
from app.services import (
    account, gating, revenuecat, subscription, subscription_periods, subscription_repair,
)
from app.services.subscription_access import EvidenceError
from app.services.subscription_processing import COHORT, ENABLED
from db.envfile import assert_dev_target, load_conn, project_ref

UTC = timezone.utc
YEARLY = "com.geniusjun.moly.plus.yearly"
SERVICE_TEST_SHA = "a" * 40


def ms(value):
    return int(value.timestamp() * 1000)


class Store:
    """All identifiers and reads are restricted to this fixture's synthetic account."""

    def __init__(self, user_id, *, early):
        self.owner = user_id
        self.down = False
        self.hold_bonus = False
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.start = self.now - timedelta(days=5)
        self.boundary = self.now + timedelta(hours=4) if early else self.now - timedelta(days=2)
        self.end = self.boundary + timedelta(days=365)
        self.rc_id = "synthetic-rc-" + uuid.uuid4().hex
        self.trial_tx = "synthetic-trial-" + uuid.uuid4().hex
        self.paid_tx = "synthetic-paid-" + uuid.uuid4().hex
        self.snapshot = {
            "id": self.rc_id, "customer_id": str(user_id), "original_customer_id": str(user_id),
            "store": "app_store", "environment": "sandbox", "gives_access": True,
            "pending_payment": False, "auto_renewal_status": "has_already_renewed" if early else "will_renew",
            "status": "trialing" if early else "active",
            "store_subscription_identifier": self.trial_tx if early else self.paid_tx,
            "current_period_starts_at": ms(self.start if early else self.boundary),
            "current_period_ends_at": ms(self.boundary if early else self.end), "ends_at": ms(self.end),
            "pending_changes": {
                "store_subscription_identifier": self.paid_tx, "status": "active", "gives_access": True,
                "pending_payment": False, "current_period_starts_at": ms(self.boundary),
                "current_period_ends_at": ms(self.end),
            } if early else None,
        }
        self.transactions = [
            {"id": self.trial_tx, "purchased_at": ms(self.start), "effective_expiration_date": ms(self.boundary),
             "product_store_identifier": YEARLY, "revenue_in_local_currency": {"gross": 0}},
            {"id": self.paid_tx, "purchased_at": ms(self.boundary), "effective_expiration_date": ms(self.end),
             "product_store_identifier": YEARLY, "revenue_in_local_currency": {"gross": 5500}},
        ]

    def check(self):
        if self.down:
            raise revenuecat.RevenueCatError("synthetic provider outage")

    async def search(self, store_tx):
        self.check()
        assert store_tx in {self.trial_tx, self.paid_tx}
        return [{"id": self.rc_id, "environment": "sandbox"}]

    async def get(self, identity):
        self.check()
        assert identity == self.rc_id
        return deepcopy(self.snapshot)

    async def txs(self, identity):
        self.check()
        assert identity == self.rc_id
        return deepcopy(self.transactions)

    async def customers(self, owner):
        self.check()
        assert owner == str(self.owner)
        if self.hold_bonus:
            raise revenuecat.RevenueCatError("synthetic bonus history outage")
        return [deepcopy(self.snapshot)]

    def install(self, monkeypatch):
        monkeypatch.setattr(revenuecat, "search_subscriptions", self.search)
        monkeypatch.setattr(revenuecat, "get_subscription", self.get)
        monkeypatch.setattr(revenuecat, "subscription_transactions", self.txs)
        monkeypatch.setattr(revenuecat, "customer_subscriptions", self.customers)

        async def forbidden(*args, **kwargs):
            raise AssertionError("real provider HTTP is forbidden")
        monkeypatch.setattr(revenuecat, "_get", forbidden)

    def event(self, kind, *, trial=False, at=None):
        event_at = at or (self.start if trial else self.boundary - timedelta(hours=8))
        return {
            "id": "synthetic-event-" + uuid.uuid4().hex, "type": kind, "app_user_id": str(self.owner),
            "environment": "SANDBOX", "store": "APP_STORE", "product_id": YEARLY,
            "original_transaction_id": self.trial_tx,
            "transaction_id": self.trial_tx if trial else self.paid_tx,
            "period_type": "TRIAL" if trial else "NORMAL", "currency": "JPY",
            "price_in_purchased_currency": 0 if trial else 5500,
            "purchased_at_ms": ms(self.start if trial else self.boundary),
            "expiration_at_ms": ms(self.boundary if trial else self.end),
            "event_timestamp_ms": ms(event_at),
        }

    def refund(self, *, outage=False):
        at = datetime.now(UTC)
        self.down = outage
        self.snapshot.update({"gives_access": False, "status": "expired", "pending_changes": None,
                              "auto_renewal_status": "will_not_renew", "current_period_ends_at": ms(at),
                              "ends_at": ms(at)})
        result = self.event("CANCELLATION", at=at)
        result.update({"cancel_reason": "CUSTOMER_SUPPORT", "price_in_purchased_currency": -5500,
                       "expiration_at_ms": ms(at)})
        return result

    def reverse(self):
        self.down = False
        self.snapshot.update({"gives_access": True, "status": "active", "pending_changes": None,
                              "current_period_ends_at": ms(self.end), "ends_at": ms(self.end),
                              "store_subscription_identifier": self.paid_tx,
                              "current_period_starts_at": ms(self.boundary)})
        return self.event("REFUND_REVERSED", at=datetime.now(UTC) + timedelta(seconds=1))


@pytest_asyncio.fixture
async def db():
    if os.environ.get("MOLY_SUBSCRIPTION_REFUND_TEST_ENV") != "dev":
        pytest.skip("explicit development database gate required")
    filename = os.environ.get("MOLY_SUBSCRIPTION_REFUND_ENV_FILE", "")
    if not Path(filename).is_absolute() or os.environ.get("MOLY_SCHEMA_TEST_DSN"):
        pytest.fail("absolute existing development env required; no local schema DSN")
    dsn = load_conn(filename)
    assert_dev_target(filename, dsn)
    engine = create_async_engine(make_url(dsn).set(drivername="postgresql+asyncpg"),
                                 connect_args={"statement_cache_size": 0, "command_timeout": 10},
                                 hide_parameters=True, pool_size=2, max_overflow=0)
    ids = []
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            await connection.execute(text("SET LOCAL lock_timeout='2s'"))
            sessions = async_sessionmaker(connection, expire_on_commit=False,
                                          join_transaction_mode="create_savepoint")
            try:
                yield SimpleNamespace(sessions=sessions, ids=ids, engine=engine, connection=connection,
                                      project_ref=project_ref(dsn))
            finally:
                await outer.rollback()
        # Verify fixture cleanup with an independent transaction, using random IDs only.
        async with engine.connect() as connection:
            for uid in ids:
                assert not (await connection.execute(text("SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=:id)"),
                                                     {"id": uid})).scalar_one()
                assert not (await connection.execute(text("SELECT EXISTS(SELECT 1 FROM public.profiles WHERE id=:id)"),
                                                     {"id": uid})).scalar_one()
    finally:
        await engine.dispose()


async def signup(db):
    uid = uuid.uuid4()
    db.ids.append(uid)
    created = datetime.now(UTC) - timedelta(days=20)
    async with db.sessions() as session:
        await session.execute(text("INSERT INTO auth.users(id,created_at) VALUES(:uid,:created)"),
                              {"uid": uid, "created": created})
        await session.execute(text("UPDATE profiles SET hay_balance=120,language='ko',timezone='Asia/Seoul',"
                                   "trial_ends_at=NULL,created_at=:created WHERE id=:uid"),
                              {"uid": uid, "created": created})
        for key, value in ((ENABLED, True), (COHORT, [str(uid)])):
            await session.execute(text("INSERT INTO app_config(key,value) VALUES(:key,CAST(:value AS jsonb)) "
                                       "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"),
                                  {"key": key, "value": json.dumps(value)})
        await session.commit()
    return uid


async def receive(db, payload):
    async with db.sessions() as session:
        return await subscription.ingest_event(session, payload["id"], payload)


async def retry(db, identity):
    async with db.sessions() as session:
        return await subscription.process_event(session, identity)


async def stored(db, store):
    async with db.sessions() as session:
        profile = await session.get(Profile, store.owner)
        sub = (await session.execute(select(Subscription).where(Subscription.user_id == store.owner))).scalar_one()
        pay = (await session.execute(select(Payment).where(Payment.store_transaction_id == store.paid_tx))).scalar_one_or_none()
        hay = list((await session.execute(select(HayTransaction).where(HayTransaction.user_id == store.owner))).scalars())
        return profile.hay_balance, sub, pay, [(h.type, h.amount) for h in hay]


async def purchase(db, monkeypatch, *, early=False, held=False):
    uid = await signup(db)
    store = Store(uid, early=early)
    store.hold_bonus = held
    store.install(monkeypatch)
    initial = store.event("INITIAL_PURCHASE", trial=True)
    assert await receive(db, initial) == subscription.HANDLED
    # Model a trial receipt already observed at its real start, before the paid
    # renewal arrived. These fixed historical event dates are not permission to
    # reconstruct an unobserved historical payer on a newly discovered object.
    async with db.sessions() as session:
        sub = (await session.execute(select(Subscription).where(Subscription.user_id == uid))).scalar_one()
        sub.ownership_changed_at = store.start
        proof = deepcopy(sub.access_evidence)
        for period in proof["periods"]:
            period["owner_start"] = store.start.isoformat()
        sub.access_evidence = proof
        await session.commit()
    paid = store.event("RENEWAL")
    result = await receive(db, paid)
    async with db.sessions() as session:
        record = await session.get(RevenuecatEvent, paid["id"])
        assert result == (subscription.HANDLED_PENDING if held else subscription.HANDLED), record.processing_state
    return store, paid


async def test_early_charge_persists_bonus_stage_and_switches_at_boundary(db, monkeypatch):
    store, paid_event = await purchase(db, monkeypatch, early=True, held=True)
    balance, sub, pay, hay = await stored(db, store)
    assert balance == 120 and hay == []
    assert pay.status == "paid" and pay.subscription_bonus_review.startswith("held:")
    assert sub.expires_at == store.end and sub.store_trial_ends_at == store.boundary
    assert sub.latest_transaction_id == store.trial_tx
    async with db.sessions() as session:
        event = await session.get(RevenuecatEvent, paid_event["id"])
        assert event.processing_state["payment_record"] == "done"
        assert event.processing_state["bonus"] == "pending"
        assert event.processing_state["access_state"] == "verified"
        now = datetime.now(UTC)
        current = await account._load_active_subscription(session, str(store.owner), now)
        boundary = await account._load_active_subscription(session, str(store.owner), store.boundary)
        historical = await subscription_periods.subscription_at(session, store.owner, store.boundary)
        assert current.store_trial_ends_at == store.boundary
        assert boundary.store_trial_ends_at is None and historical.store_trial_ends_at is None
        tier = await gating.resolve_entitlement(session, str(store.owner), store.boundary)
        assert tier["entitlement_source"] == "subscription" and tier["ads_removed"]
        response = await subscription.get_subscription(session, str(store.owner))
        assert response["in_trial"] and response["trial_ends_at"] == store.boundary.isoformat()
    store.hold_bonus = False
    assert await retry(db, paid_event["id"]) == subscription.HANDLED
    balance, _, pay, hay = await stored(db, store)
    assert balance == 4120 and pay.subscription_bonus_review == "granted"
    assert hay == [("subscription_grant", 4000)]
    assert await receive(db, paid_event) == "skipped"


async def test_refund_short_expiry_and_duplicate_refund_change_finances_once(db, monkeypatch):
    store, _ = await purchase(db, monkeypatch)
    refund = store.refund()
    assert await receive(db, refund) == subscription.HANDLED
    balance, sub, pay, hay = await stored(db, store)
    assert pay.status == "refunded" and balance == 120
    assert sub.status == "expired" and sub.expires_at < store.end
    assert sorted(hay) == sorted([("subscription_grant", 4000), ("refund_revoke", -4000)])
    assert await receive(db, refund) == "skipped"
    duplicate = {**refund, "id": "synthetic-event-" + uuid.uuid4().hex,
                 "event_timestamp_ms": refund["event_timestamp_ms"] + 1}
    assert await receive(db, duplicate) == subscription.HANDLED
    expiration = {**duplicate, "id": "synthetic-event-" + uuid.uuid4().hex, "type": "EXPIRATION",
                  "event_timestamp_ms": duplicate["event_timestamp_ms"] + 1}
    assert await receive(db, expiration) == subscription.HANDLED
    after, _, _, after_hay = await stored(db, store)
    assert after == balance and after_hay == hay
    async with db.sessions() as session:
        assert await account._load_active_subscription(session, str(store.owner), datetime.now(UTC)) is None
        assert (await subscription.get_subscription(session, str(store.owner)))["status"] == "expired"


async def test_provider_failure_commits_finance_and_only_retries_access(db, monkeypatch):
    store, _ = await purchase(db, monkeypatch)
    refund = store.refund(outage=True)
    assert await receive(db, refund) == subscription.HANDLED_PENDING
    balance, _, pay, hay = await stored(db, store)
    assert pay.status == "refunded" and balance == 120
    async with db.sessions() as session:
        row = await session.get(RevenuecatEvent, refund["id"])
        assert row.status == "pending"
        assert row.processing_state["refund_or_reversal"] == "done"
        assert row.processing_state["access_state"] == "pending"
        assert row.processing_state["finance_done"] is True
    store.down = False
    assert await retry(db, refund["id"]) == subscription.HANDLED
    after, sub, _, after_hay = await stored(db, store)
    assert sub.status == "expired" and after == balance and after_hay == hay
    async with db.sessions() as session:
        row = await session.get(RevenuecatEvent, refund["id"])
        assert row.status == "processed" and row.processing_state["access_state"] == "verified"


async def test_refund_reversed_then_old_access_retry_does_not_reclawback(db, monkeypatch):
    store, _ = await purchase(db, monkeypatch)
    refund = store.refund(outage=True)
    assert await receive(db, refund) == subscription.HANDLED_PENDING
    reverse = store.reverse()
    assert await receive(db, reverse) == subscription.HANDLED
    balance, _, pay, hay = await stored(db, store)
    assert pay.status == "paid" and balance == 4120
    assert sorted(hay) == sorted([
        ("subscription_grant", 4000), ("refund_revoke", -4000), ("admin_adjustment", 4000),
    ])
    assert await retry(db, refund["id"]) == subscription.HANDLED
    after, sub, pay, after_hay = await stored(db, store)
    assert sub.status == "active" and pay.status == "paid"
    assert after == balance and after_hay == hay
    async with db.sessions() as session:
        grant = (await session.execute(select(SubscriptionHayGrant).where(
            SubscriptionHayGrant.user_id == store.owner))).scalar_one()
        assert grant.revoked_at is None
        active = await account._load_active_subscription(session, str(store.owner), datetime.now(UTC))
        assert active is not None


async def test_bonus_held_then_refund_and_old_purchase_retry_never_grants(db, monkeypatch):
    store, paid_event = await purchase(db, monkeypatch, held=True)
    refund = store.refund()
    assert await receive(db, refund) == subscription.HANDLED
    store.hold_bonus = False
    assert await retry(db, paid_event["id"]) == subscription.HANDLED
    balance, _, pay, hay = await stored(db, store)
    assert balance == 120 and hay == [] and pay.status == "refunded"
    async with db.sessions() as session:
        event = await session.get(RevenuecatEvent, paid_event["id"])
        assert event.processing_state["payment_record"] == "done"
        assert event.processing_state["bonus"] == "skipped"
        assert event.processing_state["access_state"] == "verified"
        assert not (await session.execute(select(SubscriptionHayGrant.id).where(
            SubscriptionHayGrant.user_id == store.owner))).all()


async def test_real_advisory_lock_times_out_on_other_connection_without_writes(db):
    # A random lock key cannot block an existing customer's work or the global
    # RevenueCat ownership lock. No account or financial rows are necessary.
    key = "synthetic-lock-" + uuid.uuid4().hex
    statement = text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))")
    await db.connection.execute(statement, {"key": key})
    async with db.engine.connect() as other:
        transaction = await other.begin()
        try:
            await other.execute(text("SET LOCAL lock_timeout='150ms'"))
            with pytest.raises(DBAPIError) as caught:
                await other.execute(statement, {"key": key})
            assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        finally:
            await transaction.rollback()


async def test_legacy_null_and_json_access_use_the_same_reading_paths(db, monkeypatch):
    uid = await signup(db)
    now = datetime.now(UTC)
    async with db.sessions() as session:
        legacy = Subscription(user_id=uid, plan="monthly", status="active",
                              original_transaction_id="synthetic-legacy-" + uuid.uuid4().hex,
                              latest_transaction_id="synthetic-last-" + uuid.uuid4().hex,
                              purchased_at=now - timedelta(days=1), expires_at=now + timedelta(days=30),
                              environment="SANDBOX", auto_renew_enabled=False)
        session.add(legacy)
        await session.commit()
        active = await account._load_active_subscription(session, str(uid), now)
        response = await subscription.get_subscription(session, str(uid))
        assert active.id == legacy.id and response["plan"] == "monthly"
        assert response["expires_at"] == legacy.expires_at.isoformat()
    store, _ = await purchase(db, monkeypatch)
    async with db.sessions() as session:
        sub = (await session.execute(select(Subscription).where(Subscription.user_id == store.owner))).scalar_one()
        proof = deepcopy(sub.access_evidence)
        # Stored compatibility fields deliberately disagree; evidence must win.
        for period in proof["periods"]:
            period["end"] = min(datetime.fromisoformat(period["end"]), datetime.now(UTC)).isoformat()
        sub.access_evidence = proof
        sub.status = "active"
        sub.expires_at = store.end
        await session.commit()
        assert await account._load_active_subscription(session, str(store.owner), datetime.now(UTC)) is None
        response = await subscription.get_subscription(session, str(store.owner))
        assert response["status"] == "expired"


async def corrupt_only_subscription_metadata(db, store, *, refunded=False):
    """Reproduce the observed class of stale legacy fields on a synthetic account."""
    async with db.sessions() as session:
        sub = (await session.execute(select(Subscription).where(Subscription.user_id == store.owner))).scalar_one()
        sub.access_evidence = None
        sub.latest_transaction_id = store.trial_tx
        sub.store_trial_ends_at = store.end
        sub.expires_at = store.end
        sub.status = "active"
        if refunded:
            assert (await session.execute(select(Payment.status).where(
                Payment.store_transaction_id == store.paid_tx))).scalar_one() == "refunded"
        await session.commit()
        return sub.id


async def make_repair_preview(db, identity, mode):
    async with db.sessions() as session:
        return await subscription_repair.preview(session, identity, mode,
                                                  project_ref=db.project_ref, app_sha=SERVICE_TEST_SHA)


async def apply_repair_preview(db, plan, *, on_status=None):
    async with db.sessions() as session:
        return await subscription_repair.apply_preview(
            session, plan, expected_sha256=subscription_repair.digest(plan),
            project_ref=db.project_ref, app_sha=SERVICE_TEST_SHA, on_status=on_status,
        )


async def test_metadata_repair_preserves_paid_access_expiry_and_all_finances(db, monkeypatch):
    store, _ = await purchase(db, monkeypatch)
    identity = await corrupt_only_subscription_metadata(db, store)
    balance, before_sub, _, before_hay = await stored(db, store)
    plan = await make_repair_preview(db, identity, "metadata")
    assert plan["before"]["latest_transaction_id"] == store.trial_tx
    assert plan["after"]["latest_transaction_id"] == store.paid_tx
    assert plan["after"]["store_trial_ends_at"] is None
    assert plan["after"]["expires_at"] == before_sub.expires_at.isoformat()
    audit = await apply_repair_preview(db, plan)
    assert audit["status"] == "committed"
    assert audit["postcheck"] == "verified" and audit["postcheck_provider_unchanged"]
    after_balance, sub, pay, after_hay = await stored(db, store)
    assert after_balance == balance and after_hay == before_hay
    assert pay.status == "paid" and sub.latest_transaction_id == store.paid_tx
    assert sub.expires_at == store.end and sub.store_trial_ends_at is None
    async with db.sessions() as session:
        active = await account._load_active_subscription(session, str(store.owner), datetime.now(UTC))
        assert active is not None and active.expires_at == store.end
        assert await subscription_repair._financial_digest(session, sub) == plan["financial_sha256"]


async def test_refunded_repair_removes_stale_access_without_financial_replay(db, monkeypatch):
    store, _ = await purchase(db, monkeypatch)
    assert await receive(db, store.refund()) == subscription.HANDLED
    identity = await corrupt_only_subscription_metadata(db, store, refunded=True)
    before_balance, _, _, before_hay = await stored(db, store)
    plan = await make_repair_preview(db, identity, "refunded")
    assert plan["after"]["status"] == "expired"
    assert plan["after"]["latest_transaction_id"] == store.paid_tx
    assert (await apply_repair_preview(db, plan))["status"] == "committed"
    after_balance, sub, pay, after_hay = await stored(db, store)
    assert after_balance == before_balance == 120 and after_hay == before_hay
    assert pay.status == "refunded" and sub.store_trial_ends_at is None
    async with db.sessions() as session:
        assert await account._load_active_subscription(session, str(store.owner), datetime.now(UTC)) is None
        assert await subscription_repair._financial_digest(session, sub) == plan["financial_sha256"]
    with pytest.raises(EvidenceError, match="baseline changed"):
        await apply_repair_preview(db, plan)
    replacement = await make_repair_preview(db, identity, "refunded")
    assert replacement["after"]["status"] == "expired"
    assert replacement["financial_sha256"] == plan["financial_sha256"]


@pytest.mark.parametrize("change", ["pending_event", "owner"])
async def test_repair_rejects_local_activity_after_preview(db, monkeypatch, change):
    store, _ = await purchase(db, monkeypatch)
    identity = await corrupt_only_subscription_metadata(db, store)
    plan = await make_repair_preview(db, identity, "metadata")
    if change == "owner":
        new_owner = await signup(db)
    async with db.sessions() as session:
        if change == "owner":
            sub = await session.get(Subscription, identity)
            sub.user_id = new_owner
        else:
            incoming = store.event("UNCANCELLATION", at=datetime.now(UTC))
            session.add(RevenuecatEvent(event_id=incoming["id"], payload=incoming, status="pending"))
        await session.commit()
    with pytest.raises(EvidenceError, match="baseline changed"):
        await apply_repair_preview(db, plan)
    async with db.sessions() as session:
        current = await session.get(Subscription, identity)
        assert current.access_evidence is None and current.latest_transaction_id == store.trial_tx
        assert current.user_id == (new_owner if change == "owner" else store.owner)


async def test_repair_commit_then_postcheck_outage_never_restores_bad_state(db, monkeypatch):
    store, _ = await purchase(db, monkeypatch)
    identity = await corrupt_only_subscription_metadata(db, store)
    plan = await make_repair_preview(db, identity, "metadata")
    statuses = []

    def status(audit):
        statuses.append(deepcopy(audit))
        if audit["status"] == "committed":
            store.down = True

    audit = await apply_repair_preview(db, plan, on_status=status)
    assert audit["status"] == "committed" and not audit["postcheck_provider_unchanged"]
    assert audit["postcheck"] == "provider_unavailable"
    assert statuses[0]["status"] == "commit_pending"
    assert statuses[1]["status"] == "committed"
    assert statuses[-1]["postcheck"] == audit["postcheck"]
    _, sub, pay, _ = await stored(db, store)
    assert sub.latest_transaction_id == store.paid_tx and sub.store_trial_ends_at is None
    assert pay.status == "paid" and sub.expires_at == store.end
