"""Committed synthetic fixtures on the existing explicitly opted-in development DB.

Unlike the rollback integration suite, two independent connections must see these
rows. Exact random IDs/email are deleted in finally, followed by a fresh-connection
zero-residue check. No normal customer data, real HTTP, models, or worker dispatch.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.models.hay_transaction import HayTransaction
from app.models.payment import Payment
from app.models.profile import Profile
from app.models.revenuecat_event import RevenuecatEvent
from app.models.subscription import Subscription
from app.models.subscription_hay_grant import SubscriptionHayGrant
from app.services import hay_ledger, revenuecat, subscription, subscription_access
from db.envfile import assert_dev_target, load_conn

UTC = timezone.utc
PRODUCT = "com.geniusjun.moly.plus.yearly"


def _ms(value):
    return int(value.timestamp() * 1000)


async def test_distinct_refund_events_revoke_actual_grant_once(monkeypatch):
    if os.environ.get("MOLY_SUBSCRIPTION_CONCURRENCY_TEST_ENV") != "dev":
        pytest.skip("explicit development committed-fixture opt-in required")
    filename = os.environ.get("MOLY_SUBSCRIPTION_REFUND_ENV_FILE", "")
    if not Path(filename).is_absolute() or os.environ.get("MOLY_SCHEMA_TEST_DSN"):
        pytest.fail("absolute development env required; schema/local DSN forbidden")
    dsn = load_conn(filename)
    assert_dev_target(filename, dsn)

    class BoundedSession(Session):
        pass

    race_connections = set()

    def bound_transaction(session, transaction, connection):
        connection.exec_driver_sql("SET LOCAL lock_timeout='2s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout='8s'")
        if session.info.get("refund_race"):
            race_connections.add(id(connection.connection.dbapi_connection))

    event.listen(BoundedSession, "after_begin", bound_transaction)
    engine = create_async_engine(make_url(dsn).set(drivername="postgresql+asyncpg"),
                                 connect_args={"statement_cache_size": 0, "command_timeout": 10},
                                 hide_parameters=True, pool_size=3, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False, sync_session_class=BoundedSession)
    token = uuid.uuid4().hex
    uid, sid, pid, gid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    email = f"subscription-refund-race-{token}@example.invalid"
    rc_id, tx_id = "synthetic-race-rc-" + token, "synthetic-race-paid-" + token
    event_ids = ["synthetic-race-refund-a-" + token, "synthetic-race-refund-b-" + token]
    now = datetime.now(UTC).replace(microsecond=0)
    created, start, end = now - timedelta(days=20), now - timedelta(days=2), now + timedelta(days=363)
    cutoff = now - timedelta(seconds=2)
    snapshot = {"id": rc_id, "customer_id": str(uid), "original_customer_id": str(uid),
                "store": "app_store", "environment": "sandbox", "gives_access": True,
                "pending_payment": False, "auto_renewal_status": "will_renew", "status": "active",
                "store_subscription_identifier": tx_id, "current_period_starts_at": _ms(start),
                "current_period_ends_at": _ms(end), "ends_at": _ms(end), "pending_changes": None}
    txs = [{"id": tx_id, "purchased_at": _ms(start), "effective_expiration_date": _ms(end),
            "product_store_identifier": PRODUCT, "revenue_in_local_currency": {"gross": 5500}}]
    purchase = {"type": "RENEWAL", "app_user_id": str(uid), "transaction_id": tx_id,
                "original_transaction_id": tx_id, "environment": "SANDBOX", "store": "APP_STORE",
                "product_id": PRODUCT, "period_type": "NORMAL", "price_in_purchased_currency": 5500,
                "currency": "JPY", "purchased_at_ms": _ms(start), "expiration_at_ms": _ms(end),
                "event_timestamp_ms": _ms(start)}
    evidence = subscription_access.build_evidence(snapshot, txs, [purchase], {PRODUCT: "yearly"},
                                                   now=now, ownership_since=created)
    terminal = deepcopy(snapshot)
    terminal.update(gives_access=False, status="expired", auto_renewal_status="will_not_renew",
                    current_period_ends_at=_ms(cutoff), ends_at=_ms(cutoff))
    terminal_txs = deepcopy(txs)
    terminal_txs[0].update(effective_expiration_date=_ms(cutoff), revenue_in_local_currency={"gross": 0})
    payloads = [{**purchase, "id": identity, "type": "CANCELLATION", "cancel_reason": "CUSTOMER_SUPPORT",
                 "event_timestamp_ms": _ms(cutoff) + index, "expiration_at_ms": _ms(cutoff)}
                for index, identity in enumerate(event_ids)]
    source_id = None
    proof = {}
    try:
        # Auth bootstrap, notifications, schedules, and all money are committed
        # together; no worker can observe a partially protected fixture.
        async with sessions() as session:
            await session.execute(text("INSERT INTO auth.users(id,email,created_at) VALUES(:uid,:email,:created)"),
                                  {"uid": uid, "email": email, "created": created})
            await session.execute(text("UPDATE profiles SET hay_balance=120,language='ko',timezone='Asia/Seoul',"
                                       "next_diary_due_at=NULL,trial_ends_at=NULL,app_trial_ends_at=NULL,"
                                       "created_at=:created WHERE id=:uid"), {"uid": uid, "created": created})
            for kind in ("morning_diary", "evening_chat"):
                await session.execute(text("INSERT INTO user_notification_settings(user_id,type,enabled) "
                                           "VALUES(:uid,:kind,false) ON CONFLICT(user_id,type) "
                                           "DO UPDATE SET enabled=false"), {"uid": uid, "kind": kind})
            await session.execute(text("DELETE FROM user_schedules WHERE user_id=:uid"), {"uid": uid})
            sub = Subscription(id=sid, user_id=uid, rc_subscription_id=rc_id, plan="yearly", status="active",
                               original_transaction_id=tx_id, latest_transaction_id=tx_id, purchased_at=start,
                               expires_at=end, store_trial_ends_at=None, environment="SANDBOX",
                               last_event_at=start, auto_renew_enabled=True, access_evidence=evidence)
            session.add(sub)
            await session.flush()
            source = await hay_ledger.apply(session, uid, "subscription_grant", 4000)
            source_id = source.id
            session.add(SubscriptionHayGrant(id=gid, user_id=uid, plan="yearly", hay_transaction_id=source.id))
            await session.flush()
            session.add(Payment(id=pid, user_id=uid, subscription_id=sid, subscription_plan="yearly",
                                subscription_hay_grant_id=gid, subscription_bonus_review="granted", store="app_store",
                                store_transaction_id=tx_id, amount=Decimal("5500"), currency="JPY", status="paid", paid_at=start))
            for payload in payloads:
                session.add(RevenuecatEvent(event_id=payload["id"], payload=payload, status="pending", attempts=0,
                                           next_attempt_at=now + timedelta(days=1)))
            await session.commit()

        # The barrier ensures both processes release their initial inbox claim
        # and prepare from the committed fixture before either applies finance.
        both_provider_reads = asyncio.Event()
        provider_reads = 0

        async def search(identity):
            assert identity == tx_id
            return [{"id": rc_id, "environment": "sandbox"}]

        async def get(identity):
            assert identity == rc_id
            return deepcopy(terminal)

        async def transactions(identity):
            nonlocal provider_reads
            assert identity == rc_id
            provider_reads += 1
            if provider_reads == 2:
                both_provider_reads.set()
            if provider_reads <= 2:
                await asyncio.wait_for(both_provider_reads.wait(), timeout=5)
            return deepcopy(terminal_txs)

        async def forbidden(*args, **kwargs):
            raise AssertionError("real provider HTTP or unrelated synthetic identity forbidden")

        async def no_google_products(session):
            return {}

        monkeypatch.setattr(revenuecat, "search_subscriptions", search)
        monkeypatch.setattr(revenuecat, "get_subscription", get)
        monkeypatch.setattr(revenuecat, "subscription_transactions", transactions)
        monkeypatch.setattr(revenuecat, "customer_subscriptions", forbidden)
        monkeypatch.setattr(revenuecat, "_get", forbidden)
        monkeypatch.setattr(subscription, "_google_products", no_google_products)

        async def process(identity):
            assert identity in event_ids
            async with sessions() as session:
                session.info["refund_race"] = True
                # These are independent checked-out SQLAlchemy connections.
                assert await session.get(Profile, uid) is not None
                return await subscription.process_event(session, identity)

        initial = await asyncio.wait_for(asyncio.gather(*(process(identity) for identity in event_ids)), timeout=20)
        assert len(race_connections) >= 2
        assert set(initial) == {subscription.HANDLED, subscription.HANDLED_PENDING}
        for _ in range(3):
            async with sessions() as session:
                pending = list((await session.scalars(select(RevenuecatEvent.event_id).where(
                    RevenuecatEvent.event_id.in_(event_ids), RevenuecatEvent.status == "pending"))).all())
            if not pending:
                break
            await asyncio.wait_for(asyncio.gather(*(process(identity) for identity in pending)), timeout=20)

        async with sessions() as session:
            pay = await session.get(Payment, pid)
            profile = await session.get(Profile, uid)
            grant = await session.get(SubscriptionHayGrant, gid)
            ledger = list((await session.scalars(select(HayTransaction).where(HayTransaction.user_id == uid))).all())
            inbox = list((await session.scalars(select(RevenuecatEvent).where(RevenuecatEvent.event_id.in_(event_ids)))).all())
            assert pay.status == "refunded" and profile.hay_balance == 120
            assert grant.revoked_at is not None
            assert [(row.type, row.amount) for row in ledger].count(("subscription_grant", 4000)) == 1
            assert [(row.type, row.amount) for row in ledger].count(("refund_revoke", -4000)) == 1
            assert len(ledger) == 2 and any(row.id == source_id for row in ledger)
            assert any(row.id == grant.clawback_hay_transaction_id and row.amount == -4000 for row in ledger)
            assert len(inbox) == 2 and all(row.status == "processed" for row in inbox)
            assert all(row.processing_state["refund_or_reversal"] in {"done", "skipped"} for row in inbox)
            assert all(row.processing_state["access_state"] == "verified" for row in inbox)
            proof.update(refund_revoke_rows=1, final_balance=profile.hay_balance, event_count=len(inbox))
    finally:
        # Cleanup is its own committed transaction even when the test fails.
        try:
            async with sessions() as session:
                await session.execute(text("DELETE FROM revenuecat_events WHERE event_id IN (:a,:b) "
                                           "AND payload->>'app_user_id'=:uid"),
                                      {"a": event_ids[0], "b": event_ids[1], "uid": str(uid)})
                await session.execute(text("DELETE FROM payments WHERE id=:pid AND user_id=:uid"), {"pid": pid, "uid": uid})
                await session.execute(text("DELETE FROM subscription_hay_grants WHERE id=:gid AND user_id=:uid"), {"gid": gid, "uid": uid})
                await session.execute(text("DELETE FROM hay_transactions WHERE user_id=:uid"), {"uid": uid})
                await session.execute(text("DELETE FROM subscriptions WHERE id=:sid AND user_id=:uid"), {"sid": sid, "uid": uid})
                await session.execute(text("DELETE FROM auth.users WHERE id=:uid AND email=:email"), {"uid": uid, "email": email})
                await session.execute(text("DELETE FROM privacy_subject_barriers WHERE user_id=:uid"), {"uid": uid})
                await session.commit()
            # New connection, not a savepoint or the fixture transaction.
            async with engine.connect() as connection:
                for table, column in (("auth.users", "id"), ("public.profiles", "id"),
                                      ("public.payments", "user_id"), ("public.subscriptions", "user_id"),
                                      ("public.subscription_hay_grants", "user_id"), ("public.hay_transactions", "user_id"),
                                      ("public.user_notification_settings", "user_id"), ("public.user_schedules", "user_id"),
                                      ("public.user_items", "user_id"), ("public.privacy_subject_barriers", "user_id"),
                                      ("public.async_jobs", "user_id"), ("public.diaries", "user_id")):
                    count = await connection.scalar(text(f"SELECT count(*) FROM {table} WHERE {column}=:uid"), {"uid": uid})
                    assert count == 0, f"synthetic fixture residue in {table}"
                assert await connection.scalar(text("SELECT count(*) FROM revenuecat_events WHERE event_id IN (:a,:b)"),
                                               {"a": event_ids[0], "b": event_ids[1]}) == 0
                assert await connection.scalar(text("SELECT count(*) FROM auth.users WHERE email=:email"), {"email": email}) == 0
            print("committed synthetic dev concurrency: cleanup verified, no fixture rows remain")
        finally:
            await engine.dispose()
            event.remove(BoundedSession, "after_begin", bound_transaction)
    assert proof == {"refund_revoke_rows": 1, "final_balance": 120, "event_count": 2}
