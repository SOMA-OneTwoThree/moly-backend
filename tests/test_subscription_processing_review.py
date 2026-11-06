"""Independent regression cases; in-memory doubles only, no provider or DB calls."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.services import subscription, subscription_processing as processing


class HistorySession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, statement):
        return self

    def scalars(self):
        return iter(self.rows)


def event(identifier, action, seconds):
    return {"id": identifier, "transaction_id": "review-transaction",
            "type": "REFUND_REVERSED" if action == "reverse" else "CANCELLATION",
            "cancel_reason": "CUSTOMER_SUPPORT", "event_timestamp_ms": seconds * 1000}


def history(identifier, action, seconds, *, status="processed", stage="done"):
    return SimpleNamespace(event_id=identifier, payload=event(identifier, action, seconds),
                           status=status, processing_state={"refund_or_reversal": stage})


@pytest.mark.parametrize("status", ["pending", "failed"])
async def test_review_committed_finance_counts_even_when_access_not_processed(status):
    session = HistorySession([history("refund", "refund", 10),
                              history("reverse", "reverse", 30, status=status)])
    pay = SimpleNamespace(store_transaction_id="review-transaction", status="paid")
    assert await processing._financial_order(session, event("old-refund", "refund", 20), pay, "refund") == "skip"


async def test_review_newer_same_action_must_block_delayed_opposite_action(monkeypatch):
    rows = [history("first-refund", "refund", 10)]
    session = HistorySession(rows)
    pay = SimpleNamespace(id=uuid.uuid4(), store_transaction_id="review-transaction", status="refunded",
                          subscription_bonus_review="not_granted:user_plan_previously_granted",
                          subscription_hay_grant_id=None)
    monkeypatch.setattr(subscription, "_refund_subscription", AsyncMock())
    newer = event("newer-refund", "refund", 30)
    state = processing.new_state(newer)
    prepared = processing.Prepared([], None, datetime.now(timezone.utc))
    await processing._finance(session, newer, state, pay, None, prepared)
    rows.append(SimpleNamespace(event_id=newer["id"], payload=newer,
                                status="processed", processing_state=state))
    # Actual order: refund(10), reversal(20), refund(30). The final refund's
    # no-money-change observation still supersedes the delayed reversal.
    assert await processing._financial_order(session, event("delayed-reversal", "reverse", 20), pay, "reverse") != "apply"


async def test_review_legacy_reversal_marker_without_history_does_not_allow_recollection():
    pay = SimpleNamespace(store_transaction_id="review-transaction", status="paid",
                          subscription_bonus_review="granted:refund_reversed")
    decision = await processing._financial_order(HistorySession([]), event("old-refund", "refund", 20), pay, "refund")
    assert decision == "manual"


async def test_review_same_timestamp_opposing_finance_is_manual():
    pay = SimpleNamespace(store_transaction_id="review-transaction", status="refunded")
    decision = await processing._financial_order(HistorySession([history("refund", "refund", 10)]),
                                                event("reverse", "reverse", 10), pay, "reverse")
    assert decision == "manual"


@pytest.mark.parametrize("access_stage", ["manual", "verified"])
async def test_review_baseline_conflict_preserves_completed_access_stage(monkeypatch, access_stage):
    uid, sid = uuid.uuid4(), uuid.uuid4()
    before = {"id": sid, "user_id": uid}
    prepared = processing.Prepared([before], None, datetime.now(timezone.utc))
    payload = {"id": "conflict", "type": "RENEWAL", "app_user_id": str(uid)}
    state = processing.new_state(payload)
    state["access_state"] = access_stage
    row = SimpleNamespace(payload=payload, processing_state=state)
    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=sid, user_id=uid)))
    monkeypatch.setattr(subscription, "_ownership_lock", AsyncMock())
    monkeypatch.setattr(processing, "advisory_xact_lock", AsyncMock())
    await processing.apply(session, row, prepared)
    assert row.processing_state["access_state"] == access_stage


def test_review_finish_attempt_never_retries_manual_or_completed_stages():
    state = processing.new_state({"type": "RENEWAL"})
    state.update(payment_record="done", bonus="manual", access_state="verified")
    status, pending = processing.finish_attempt(state)
    assert (status, pending) == ("failed", False)
    assert state["tries"] == {}


async def test_review_refunded_held_purchase_never_gets_new_bonus(monkeypatch):
    uid = uuid.uuid4()
    payload = {"type": "RENEWAL", "app_user_id": str(uid)}
    state = processing.new_state(payload)
    state["payment_record"] = "done"
    pay = SimpleNamespace(id=uuid.uuid4(), user_id=uid, status="refunded", subscription_bonus_review="held:provider_history_unavailable")
    sub = SimpleNamespace(user_id=uid, rc_subscription_id="review-object")
    prepared = processing.Prepared([], None, datetime.now(timezone.utc),
                                   targets=[SimpleNamespace(snapshot={"id": "review-object"})],
                                   bonus_review="eligible")
    grant = AsyncMock()
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    await processing._finance(HistorySession([]), payload, state, pay, sub, prepared)
    assert state["bonus"] == "skipped"
    grant.assert_not_awaited()


async def test_review_manual_access_stage_is_not_automatically_applied_on_bonus_retry(monkeypatch):
    uid, sid = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    sub = SimpleNamespace(id=sid, user_id=uid)
    before = processing.baseline(sub)
    target = processing.Target(before, {"id": "review-object", "customer_id": str(uid)},
                               [{"id": "review-transaction"}],
                               {"verified_at": now.isoformat(), "revision": 1})
    prepared = processing.Prepared([before], None, now, targets=[target])
    payload = {"id": "manual-access", "type": "RENEWAL", "transaction_id": "review-transaction", "app_user_id": str(uid)}
    state = processing.new_state(payload)
    state["access_state"] = "manual"
    row = SimpleNamespace(payload=payload, processing_state=state)

    async def get(model, identifier, **kwargs):
        return sub if identifier == sid else SimpleNamespace(id=uid)

    session = SimpleNamespace(get=get)
    monkeypatch.setattr(subscription, "_ownership_lock", AsyncMock())
    monkeypatch.setattr(processing, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(processing, "_finance", AsyncMock())
    project = Mock()
    monkeypatch.setattr(processing.access, "project", project)
    await processing.apply(session, row, prepared)
    project.assert_not_called()


async def test_review_provider_failure_commits_refund_finance_and_pending_access(monkeypatch):
    uid, sid = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    sub = SimpleNamespace(**{name: None for name in processing._FIELDS})
    sub.id, sub.user_id, sub.environment = sid, uid, "SANDBOX"
    pay = SimpleNamespace(**{name: None for name in processing._PAY_FIELDS})
    pay.id, pay.user_id, pay.subscription_id = uuid.uuid4(), uid, sid
    pay.store_transaction_id, pay.status, pay.store = "review-transaction", "paid", "app_store"
    pay.subscription_bonus_review = "not_granted:user_plan_previously_granted"
    payload = event("provider-failure", "refund", 20)
    payload.update(app_user_id=str(uid), store="APP_STORE", environment="SANDBOX")
    row = SimpleNamespace(payload=payload, status="pending", processing_state=None, attempts=0)
    prepared = processing.Prepared([processing.baseline(sub)],
                                   {name: getattr(pay, name) for name in processing._PAY_FIELDS},
                                   now, error="provider unavailable")

    class Session:
        rollback = AsyncMock()
        commit = AsyncMock()

        async def execute(self, statement):
            return self

        def scalars(self):
            return self

        def first(self):
            return row

        async def get(self, model, identifier, **kwargs):
            return sub if model is processing.Subscription else pay

    async def refund(session, target, event_at, **kwargs):
        target.status = "refunded"

    monkeypatch.setattr(processing, "selected", AsyncMock(return_value=prepared))
    monkeypatch.setattr(processing, "fetch", AsyncMock(return_value=prepared))
    monkeypatch.setattr(processing, "_financial_order", AsyncMock(return_value="apply"))
    monkeypatch.setattr(subscription, "_ownership_lock", AsyncMock())
    monkeypatch.setattr(processing, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(subscription, "_refund_subscription", refund)
    session = Session()
    outcome = await subscription.process_event(session, payload["id"])
    assert outcome == subscription.HANDLED_PENDING
    assert pay.status == "refunded"
    assert row.status == "pending"
    assert row.processing_state["refund_or_reversal"] == "done"
    assert row.processing_state["access_state"] == "pending"
    assert row.processing_state["tries"] == {"access_state": 1}
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("bad_state", [
    {},
    {"version": 1, "payment_record": "bogus", "bonus": "not_applicable",
     "refund_or_reversal": "pending", "access_state": "verified", "tries": {}},
    {"version": 1, "payment_record": "not_applicable", "bonus": "not_applicable",
     "refund_or_reversal": "pending", "access_state": "verified", "tries": {"access_state": -1}},
])
async def test_review_invalid_nonnull_state_never_falls_back_to_legacy(monkeypatch, bad_state):
    class EmptySession:
        async def execute(self, statement):
            return self

        def scalars(self):
            return self

        def first(self):
            return None

        def __iter__(self):
            return iter(())

    monkeypatch.setattr(processing, "get_config_values", AsyncMock(return_value={processing.ENABLED: False}))
    payload = event("invalid-state", "refund", 20)
    payload["app_user_id"] = str(uuid.uuid4())
    with pytest.raises(processing.access.EvidenceError):
        await processing.selected(EmptySession(), payload, bad_state)


async def test_review_missing_payment_zero_refund_amount_is_not_trial_proof():
    payload = event("missing-payment", "refund", 20)
    payload.update(period_type="NORMAL", price_in_purchased_currency=0)
    state = processing.new_state(payload)
    prepared = processing.Prepared([], None, datetime.now(timezone.utc), error="provider unavailable")
    await processing._finance(HistorySession([]), payload, state, None, None, prepared)
    assert state["refund_or_reversal"] in {"pending", "manual"}


async def test_review_late_purchase_must_not_grant_bonus_over_unresolved_refund(monkeypatch):
    uid, sid = uuid.uuid4(), uuid.uuid4()
    refund = event("earlier-delivered-refund", "refund", 20)
    refund.update(environment="SANDBOX", store="APP_STORE", app_user_id=str(uid))
    unresolved = processing.new_state(refund)
    unresolved.update(refund_or_reversal="manual", access_state="verified")
    rows = [SimpleNamespace(event_id=refund["id"], payload=refund, status="failed", processing_state=unresolved)]
    session = HistorySession(rows)
    purchase = {"id": "delayed-original-purchase", "type": "RENEWAL", "app_user_id": str(uid),
                "transaction_id": "review-transaction", "event_timestamp_ms": 10000,
                "purchased_at_ms": 10000, "period_type": "NORMAL", "price_in_purchased_currency": 500,
                "environment": "SANDBOX", "store": "APP_STORE"}
    sub = SimpleNamespace(id=sid, user_id=uid, rc_subscription_id="review-object", ownership_changed_at=None)
    pay = SimpleNamespace(id=uuid.uuid4(), user_id=uid, subscription_id=sid, status="paid",
                          store_transaction_id="review-transaction", store="app_store",
                          subscription_plan="monthly", subscription_bonus_review="held:review_pending")
    prepared = processing.Prepared([], None, datetime.now(timezone.utc),
                                   targets=[processing.Target({"id": sid},
                                       {"id": "review-object", "customer_id": str(uid), "original_customer_id": str(uid)},
                                       [{"id": "review-transaction", "purchased_at": 10000}], {})],
                                   bonus_review="eligible")
    monkeypatch.setattr(subscription, "_record_subscription_payment", AsyncMock(return_value=pay))
    monkeypatch.setattr(subscription, "_validate_subscription_payment", Mock())
    grant = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    await processing._finance(session, purchase, processing.new_state(purchase), None, sub, prepared)
    grant.assert_not_awaited()


def test_review_fifth_failure_preserves_finance_and_requires_manual_access():
    state = processing.new_state(event("refund", "refund", 20))
    state["refund_or_reversal"] = "done"
    for attempt in range(1, 6):
        status, pending = processing.finish_attempt(state)
        assert state["refund_or_reversal"] == "done"
        if attempt < 5:
            assert (status, pending) == ("pending", True)
        else:
            assert (status, pending) == ("failed", False)
            assert state["access_state"] == "manual"
            assert state["finance_done"] is True


@pytest.mark.parametrize("owner_verified", [False, None])
async def test_review_new_payment_requires_provider_period_and_current_owner(monkeypatch, owner_verified):
    uid = uuid.uuid4()
    purchase = {"id": "unverified-purchase", "type": "RENEWAL", "app_user_id": str(uid),
                "transaction_id": "review-transaction", "period_type": "NORMAL",
                "price_in_purchased_currency": 500}
    state = processing.new_state(purchase)
    sub = SimpleNamespace(user_id=uid, rc_subscription_id="review-object")
    targets = [] if owner_verified is None else [SimpleNamespace(snapshot={
        "id": "review-object", "customer_id": str(uuid.uuid4()),
    })]
    prepared = processing.Prepared([], None, datetime.now(timezone.utc), targets=targets,
                                   bonus_review="eligible")
    create = AsyncMock()
    grant = AsyncMock()
    monkeypatch.setattr(subscription, "_record_subscription_payment", create)
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    await processing._finance(HistorySession([]), purchase, state, None, sub, prepared)
    create.assert_not_awaited()
    grant.assert_not_awaited()
    assert state["payment_record"] == state["bonus"] == "pending"


async def test_review_new_expired_subscription_can_have_no_access_periods(monkeypatch):
    uid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    evidence = {"version": 1, "revision": 1, "verified_at": now.isoformat(),
                "rc_subscription_id": "review-object", "owner_id": str(uid),
                "store": "app_store", "environment": "SANDBOX", "current_transaction_id": "review-transaction",
                "periods": [], "legacy_history": []}
    target = processing.Target(None, {"id": "review-object", "customer_id": str(uid),
                                      "store_subscription_identifier": "review-transaction", "gives_access": False},
                               [{"id": "review-transaction", "purchased_at": 10000,
                                 "product_store_identifier": "com.geniusjun.moly.plus.monthly"}], evidence)
    prepared = processing.Prepared([], None, now, targets=[target])
    payload = event("refund-before-start", "refund", 20)
    payload["app_user_id"] = str(uid)
    row = SimpleNamespace(payload=payload, processing_state=None)

    class Session:
        def __init__(self):
            self.added = []

        async def get(self, model, identifier, **kwargs):
            return SimpleNamespace(id=uid)

        async def execute(self, statement):
            return self

        def scalar_one_or_none(self):
            return None

        def add(self, value):
            self.added.append(value)

    monkeypatch.setattr(subscription, "_ownership_lock", AsyncMock())
    monkeypatch.setattr(subscription, "_resolve_plan", AsyncMock(return_value=("monthly", "com.geniusjun.moly.plus.monthly")))
    monkeypatch.setattr(processing, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(processing.access, "project", Mock())
    monkeypatch.setattr(processing, "_finance", AsyncMock())
    await processing.apply(Session(), row, prepared)


async def test_review_first_local_row_after_transfer_does_not_recreate_old_payer_money(monkeypatch):
    original_owner, current_owner = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    purchased = now - timedelta(hours=1)
    period = {"transaction_id": "review-transaction", "plan": "monthly", "kind": "paid",
              "owner_id": str(current_owner), "owner_start": now.isoformat(),
              "start": purchased.isoformat(), "end": (now + timedelta(days=29)).isoformat()}
    evidence = {"version": 1, "revision": 1, "verified_at": now.isoformat(),
                "rc_subscription_id": "review-object", "owner_id": str(current_owner), "current_transaction_id": "review-transaction",
                "environment": "SANDBOX", "store": "app_store", "periods": [period]}
    snapshot = {"id": "review-object", "customer_id": str(current_owner),
                "original_customer_id": str(original_owner), "store_subscription_identifier": "review-transaction"}
    txs = [{"id": "review-transaction", "purchased_at": int(purchased.timestamp() * 1000),
            "product_store_identifier": "com.geniusjun.moly.plus.monthly"}]
    prepared = processing.Prepared([], None, now,
                                   targets=[processing.Target(None, snapshot, txs, evidence)], bonus_review="eligible")
    payload = {"id": "restored-old-transaction", "type": "RENEWAL", "app_user_id": str(current_owner),
                "transaction_id": "review-transaction", "environment": "SANDBOX", "store": "APP_STORE",
                "event_timestamp_ms": int(now.timestamp() * 1000), "purchased_at_ms": int(purchased.timestamp() * 1000),
                "period_type": "NORMAL", "price_in_purchased_currency": 500}
    row = SimpleNamespace(payload=payload, processing_state=None)

    class Session:
        async def get(self, model, identifier, **kwargs):
            return SimpleNamespace(id=current_owner)

        async def execute(self, statement):
            return self

        def scalar_one_or_none(self):
            return None

        def scalars(self):
            return iter(())

        def add(self, value):
            pass

    def project(sub, proof, at):
        sub.rc_subscription_id = proof["rc_subscription_id"]

    async def record(session, sub, event):
        return SimpleNamespace(id=uuid.uuid4(), user_id=sub.user_id, subscription_id=sub.id,
                               store_transaction_id="review-transaction", status="paid", subscription_plan="monthly",
                               subscription_bonus_review="held:review_pending")

    monkeypatch.setattr(subscription, "_ownership_lock", AsyncMock())
    monkeypatch.setattr(subscription, "_resolve_plan", AsyncMock(return_value=("monthly", "com.geniusjun.moly.plus.monthly")))
    monkeypatch.setattr(processing, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(processing.access, "project", project)
    create = AsyncMock(side_effect=record)
    monkeypatch.setattr(subscription, "_record_subscription_payment", create)
    monkeypatch.setattr(subscription, "_validate_subscription_payment", Mock())
    grant = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    await processing.apply(Session(), row, prepared)
    create.assert_not_awaited()
    grant.assert_not_awaited()
    assert row.processing_state["payment_record"] == row.processing_state["bonus"] == "manual"


@pytest.mark.parametrize("incoming_owner_is_current", [True, False])
async def test_review_split_rc_object_routes_sticky_with_feature_disabled(monkeypatch, incoming_owner_is_current):
    old_owner, current_owner = uuid.uuid4(), uuid.uuid4()
    sub = SimpleNamespace(**{key: None for key in processing._FIELDS})
    sub.id, sub.user_id = uuid.uuid4(), current_owner
    sub.original_transaction_id = sub.latest_transaction_id = "new-rc-object-first-transaction"
    sub.access_evidence = {"version": 1, "store_original_transaction_ids": ["apple-chain-root"], "periods": []}
    incoming_owner = current_owner if incoming_owner_is_current else old_owner
    payload = {"id": "next-renewal", "type": "RENEWAL", "app_user_id": str(incoming_owner),
               "transaction_id": "brand-new-renewal-transaction", "original_transaction_id": "apple-chain-root"}
    statements = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def first(self):
            return self.rows[0] if self.rows else None

        def __iter__(self):
            return iter(self.rows)

    class Session:
        async def execute(self, statement):
            if statement.column_descriptions[0]["entity"] is processing.Payment:
                return Result([])
            statements.append(statement)
            return Result([sub])

    config = AsyncMock(return_value={processing.ENABLED: False})
    monkeypatch.setattr(processing, "get_config_values", config)
    prepared = await processing.selected(Session(), payload, None)
    assert prepared is not None
    assert prepared.before[0]["id"] == sub.id
    config.assert_not_awaited()
    # SQL contract: these rows cannot be found by incoming tx/original columns.
    # Verify both current-user adoption and retained Apple chain predicates are
    # actually sent to PostgreSQL, including after transfer to another app user.
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql, params = str(compiled), list(compiled.params.values())
    assert "subscriptions.user_id IN" in sql and "subscriptions.access_evidence IS NOT NULL" in sql
    assert "@>" in sql
    assert "store_original_transaction_ids" in params
    assert any(value == ["apple-chain-root"] for value in params)


@pytest.mark.parametrize("handler", ["_refund_subscription", "_reverse_subscription_refund"])
async def test_review_legacy_finance_entry_cannot_mutate_adopted_subscription(monkeypatch, handler):
    sub = SimpleNamespace(id=uuid.uuid4(), access_evidence={"version": 1}, status="active")
    pay = SimpleNamespace(subscription_id=sub.id, status="paid")
    session = SimpleNamespace(get=AsyncMock(return_value=sub))
    ledger = AsyncMock()
    monkeypatch.setattr(subscription.hay_ledger, "apply", ledger)
    with pytest.raises(subscription._HandlerSignal) as caught:
        await getattr(subscription, handler)(session, pay, datetime.now(timezone.utc))
    assert caught.value.outcome == subscription.DEPENDENCY_MISSING
    assert sub.status == "active" and pay.status == "paid"
    ledger.assert_not_awaited()


async def test_review_nested_malformed_provider_amount_becomes_retryable_error(monkeypatch):
    uid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    start, end = int((now - timedelta(hours=1)).timestamp() * 1000), int((now + timedelta(days=29)).timestamp() * 1000)
    snapshot = {"id": "review-object", "customer_id": str(uid), "store": "app_store", "environment": "SANDBOX",
                "gives_access": False, "store_subscription_identifier": "review-transaction",
                "current_period_starts_at": start, "current_period_ends_at": end}
    transactions = [{"id": "review-transaction", "product_store_identifier": "com.geniusjun.moly.plus.monthly",
                     "purchased_at": start, "effective_expiration_date": end,
                     "revenue_in_local_currency": "invalid-nested-object"}]
    monkeypatch.setattr(subscription, "_snapshot_for_transaction", AsyncMock(return_value=(snapshot, transactions)))
    monkeypatch.setattr(subscription, "_google_products", AsyncMock(return_value={}))
    prepared = processing.Prepared([], None, now)
    outcome = await processing.fetch(HistorySession([]), event("nested-invalid", "refund", 20), prepared)
    assert outcome.error is not None
    assert outcome.targets == []


async def test_review_confirmed_refund_with_missing_source_ledger_stays_manual(monkeypatch):
    uid = uuid.uuid4()
    pay = SimpleNamespace(id=uuid.uuid4(), user_id=uid, status="refunded", subscription_plan="monthly",
                          subscription_bonus_review="granted", subscription_hay_grant_id=uuid.uuid4())
    grant = SimpleNamespace(user_id=uid, plan="monthly", revoked_at=datetime.now(timezone.utc),
                            hay_transaction_id=None, clawback_hay_transaction_id=None)
    session = SimpleNamespace(get=AsyncMock(return_value=grant))
    monkeypatch.setattr(processing, "_financial_order", AsyncMock(return_value="confirm"))
    payload = event("confirm-missing-ledger", "refund", 20)
    state = processing.new_state(payload)
    await processing._finance(session, payload, state, pay, None,
                              processing.Prepared([], None, datetime.now(timezone.utc)))
    assert state["refund_or_reversal"] == "done"
    assert state["bonus"] == "manual"


async def test_review_prior_stage_without_access_evidence_survives_flag_off(monkeypatch):
    uid = uuid.uuid4()
    statements = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def first(self):
            return self.rows[0] if self.rows else None

        def __iter__(self):
            return iter(self.rows)

    class Session:
        async def execute(self, statement):
            if statement.column_descriptions[0]["entity"] is processing.RevenuecatEvent:
                statements.append(statement)
                return Result(["committed-finance-stage-with-provider-down"])
            return Result([])

    config = AsyncMock(return_value={processing.ENABLED: False})
    monkeypatch.setattr(processing, "get_config_values", config)
    payload = {"type": "RENEWAL", "app_user_id": str(uid), "transaction_id": "new-period",
                "original_transaction_id": "old-store-chain"}
    assert await processing.selected(Session(), payload, None) is not None
    config.assert_not_awaited()
    compiled = statements[0].compile(dialect=postgresql.dialect())
    assert "revenuecat_events.processing_state IS NOT NULL" in str(compiled)
    assert any(value == [str(uid)] for value in compiled.params.values())


@pytest.mark.parametrize("wrong_owner", [True, False])
async def test_review_refund_records_money_but_does_not_touch_unrelated_bonus(monkeypatch, wrong_owner):
    uid = uuid.uuid4()
    pay = SimpleNamespace(id=uuid.uuid4(), user_id=uid, status="paid", subscription_plan="monthly",
                          subscription_bonus_review="granted", subscription_hay_grant_id=uuid.uuid4())
    grant = SimpleNamespace(user_id=uuid.uuid4() if wrong_owner else uid,
                            plan="monthly" if wrong_owner else "yearly", revoked_at=None)
    session = SimpleNamespace(get=AsyncMock(return_value=grant))
    monkeypatch.setattr(processing, "_financial_order", AsyncMock(return_value="apply"))
    ledger = AsyncMock()
    monkeypatch.setattr(subscription.hay_ledger, "apply", ledger)
    payload = event("refund-unrelated-bonus", "refund", 20)
    state = processing.new_state(payload)
    state["access_state"] = "verified"
    await processing._finance(session, payload, state, pay, None,
                              processing.Prepared([], None, datetime.now(timezone.utc)))
    assert pay.status == "refunded"
    assert state["refund_or_reversal"] == "done" and state["bonus"] == "manual"
    assert processing.finish_attempt(state) == ("failed", False)
    assert state["finance_done"] is False
    assert grant.revoked_at is None
    ledger.assert_not_awaited()


# ── 결제 직후 받은 현재 소유자 결제(체험 만료 뒤 재구독 등) ─────────────────────────
LIVE_ANON = "$RCAnonymousID:0123456789abcdef"
LIVE_PAID = datetime(2026, 11, 6, 0, 48, 50, tzinfo=timezone.utc)
LIVE_TRIAL = datetime(2026, 11, 2, 23, 3, 41, tzinfo=timezone.utc)


def _ms(moment):
    return int(moment.timestamp() * 1000)


def _live_case(uid, *, before=None, kind="RENEWAL"):
    tx = "chain-otx" if kind == "INITIAL_PURCHASE" else "renew-tx"
    paid = LIVE_TRIAL if kind == "INITIAL_PURCHASE" else LIVE_PAID
    purchase = {"id": "live-purchase", "type": kind, "app_user_id": str(uid), "original_app_user_id": LIVE_ANON,
                "transaction_id": tx, "original_transaction_id": "chain-otx", "period_type": "NORMAL",
                "is_family_share": False, "price_in_purchased_currency": 980, "currency": "JPY",
                "environment": "PRODUCTION", "store": "APP_STORE",
                "purchased_at_ms": _ms(paid), "event_timestamp_ms": _ms(paid) + 8000}
    sub = SimpleNamespace(id=uuid.uuid4(), user_id=uid, rc_subscription_id="live-object",
                          ownership_changed_at=paid + timedelta(minutes=17))
    target = processing.Target(before, {"id": "live-object", "customer_id": str(uid),
                                        "original_customer_id": LIVE_ANON, "store_subscription_identifier": tx},
                               [{"id": tx, "purchased_at": _ms(paid)}], {})
    prepared = processing.Prepared([], None, datetime.now(timezone.utc), targets=[target], bonus_review="eligible")
    return purchase, sub, prepared, paid + timedelta(seconds=8)


def _origin_row(uid, **changes):
    payload = {"type": "INITIAL_PURCHASE", "app_user_id": str(uid), "transaction_id": "chain-otx",
               "original_transaction_id": "chain-otx", "is_family_share": False, "period_type": "TRIAL",
               "environment": "PRODUCTION", "store": "APP_STORE",
               "purchased_at_ms": _ms(LIVE_TRIAL), "event_timestamp_ms": _ms(LIVE_TRIAL) + 6000}
    payload.update(changes.pop("payload", {}))
    return SimpleNamespace(payload=payload, status=changes.get("status", "processed"),
                           received_at=changes.get("received_at", LIVE_TRIAL + timedelta(seconds=6)))


def _live_env(monkeypatch, uid, *, origins=None, transfer=False, conflict=False):
    monkeypatch.setattr(processing, "_chain_origin_rows",
                        AsyncMock(return_value=[_origin_row(uid)] if origins is None else origins))
    monkeypatch.setattr(processing, "_transfer_involving", AsyncMock(return_value=transfer))
    monkeypatch.setattr(processing, "_chain_conflict", AsyncMock(return_value=conflict))
    monkeypatch.setattr(processing, "_chain_transaction_ids",
                        AsyncMock(return_value=["chain-otx", "middle-renewal-tx", "renew-tx"]))
    pay = SimpleNamespace(id=uuid.uuid4(), user_id=uid, status="paid", store_transaction_id="renew-tx",
                          store="app_store", subscription_plan="monthly",
                          subscription_bonus_review="held:review_pending", subscription_hay_grant_id=None)
    create = AsyncMock(return_value=pay)
    grant = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    monkeypatch.setattr(subscription, "_record_subscription_payment", create)
    monkeypatch.setattr(subscription, "_validate_subscription_payment", Mock())
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    return create, grant, pay


@pytest.mark.parametrize("before", [None, {"id": "row-made-by-first-attempt"}])
async def test_live_owner_resubscribe_records_payment_and_first_hay_on_first_and_retry(monkeypatch, before):
    uid = uuid.uuid4()
    purchase, sub, prepared, received = _live_case(uid, before=before)
    create, grant, pay = _live_env(monkeypatch, uid)
    state = processing.new_state(purchase)
    await processing._finance(HistorySession([]), purchase, state, None, sub, prepared, received_at=received)
    create.assert_awaited_once()
    assert grant.await_args.args[1:] == (uid, "monthly")
    assert state["payment_record"] == "done" and state["bonus"] == "done"
    assert pay.subscription_bonus_review == "granted"
    # 충돌 확인은 묶음의 중간 갱신 거래까지 포함한 전체 거래 ID로 한다.
    assert processing._chain_conflict.await_args.args[2] == ["chain-otx", "middle-renewal-tx", "renew-tx"]


async def test_duplicate_origin_rows_of_the_same_owner_still_qualify(monkeypatch):
    uid = uuid.uuid4()
    purchase, sub, prepared, received = _live_case(uid)
    create, _, _ = _live_env(monkeypatch, uid, origins=[_origin_row(uid, status="pending"), _origin_row(uid)])
    state = processing.new_state(purchase)
    await processing._finance(HistorySession([]), purchase, state, None, sub, prepared, received_at=received)
    create.assert_awaited_once()


async def test_chain_first_purchase_is_its_own_origin_proof(monkeypatch):
    uid = uuid.uuid4()
    purchase, sub, prepared, received = _live_case(uid, kind="INITIAL_PURCHASE")
    create, _, _ = _live_env(monkeypatch, uid, origins=[])
    origin_query = processing._chain_origin_rows
    state = processing.new_state(purchase)
    await processing._finance(HistorySession([]), purchase, state, None, sub, prepared, received_at=received)
    create.assert_awaited_once()
    origin_query.assert_not_awaited()


@pytest.mark.parametrize("case", [
    "no_origin", "origin_other_user", "origin_late", "origin_family", "origin_not_processed",
    "origin_other_user_alongside", "origin_env_mismatch", "origin_store_mismatch",
    "late_event", "late_receipt", "no_receipt", "other_app_user", "snapshot_other_customer",
    "historical_transaction", "family_share", "transfer", "conflict", "free", "product_change",
])
async def test_live_owner_rule_blocks_every_unproven_case(monkeypatch, case):
    uid = uuid.uuid4()
    purchase, sub, prepared, received = _live_case(uid)
    snapshot = prepared.targets[0].snapshot
    origins = None
    if case == "no_origin":
        origins = []
    elif case == "origin_other_user":
        origins = [_origin_row(uid, payload={"app_user_id": str(uuid.uuid4())})]
    elif case == "origin_late":
        origins = [_origin_row(uid, received_at=LIVE_TRIAL + timedelta(hours=2))]
    elif case == "origin_family":
        origins = [_origin_row(uid, payload={"is_family_share": True})]
    elif case == "origin_not_processed":
        origins = [_origin_row(uid, status="pending")]
    elif case == "origin_other_user_alongside":
        origins = [_origin_row(uid), _origin_row(uid, payload={"app_user_id": str(uuid.uuid4())})]
    elif case == "origin_env_mismatch":
        origins = [_origin_row(uid, payload={"environment": "SANDBOX"})]
    elif case == "origin_store_mismatch":
        origins = [_origin_row(uid, payload={"store": "PLAY_STORE"})]
    elif case == "late_event":
        purchase["event_timestamp_ms"] = purchase["purchased_at_ms"] + 2 * 3600 * 1000
    elif case == "late_receipt":
        received = LIVE_PAID + timedelta(hours=2)
    elif case == "no_receipt":
        received = None
    elif case == "other_app_user":
        purchase["app_user_id"] = str(uuid.uuid4())
    elif case == "snapshot_other_customer":
        snapshot["customer_id"] = str(uuid.uuid4())
    elif case == "historical_transaction":
        snapshot["store_subscription_identifier"] = "newer-tx"
    elif case == "family_share":
        purchase["is_family_share"] = True
    elif case == "free":
        purchase["price_in_purchased_currency"] = 0
    elif case == "product_change":
        purchase["type"] = "PRODUCT_CHANGE"
    create, grant, _ = _live_env(monkeypatch, uid, origins=origins,
                                 transfer=case == "transfer", conflict=case == "conflict")
    state = processing.new_state(purchase)
    await processing._finance(HistorySession([]), purchase, state, None, sub, prepared, received_at=received)
    create.assert_not_awaited()
    grant.assert_not_awaited()


@pytest.mark.parametrize("created_s,received_s,ok", [
    (0, 0, True), (3600, 3600, True), (3601, 10, False), (10, 3601, False), (-1, 10, False), (10, -1, False),
])
def test_live_delivery_window(created_s, received_s, ok):
    payload = {"purchased_at_ms": _ms(LIVE_PAID), "event_timestamp_ms": _ms(LIVE_PAID) + created_s * 1000}
    assert processing._live_delivery(payload, LIVE_PAID + timedelta(seconds=received_s)) is ok


class _Capture:
    def __init__(self, rows=None, scalar=None):
        self.statements, self.rows, self.scalar = [], rows or [], scalar

    async def execute(self, statement):
        self.statements.append(statement)
        rows, scalar = self.rows, self.scalar
        return SimpleNamespace(all=lambda: rows, scalars=lambda: iter(rows), scalar_one_or_none=lambda: scalar)


def _sql(statement):
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def test_transfer_check_reads_every_status_and_filters_time_in_the_query():
    owner = str(uuid.uuid4())
    session = _Capture(scalar="transfer-event")
    assert await processing._transfer_involving(session, owner, LIVE_TRIAL) is True
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql, values = str(compiled), set(map(str, compiled.params.values()))
    assert {"TRANSFER", "transferred_to", "transferred_from"} <= values and sql.count("@>") == 2
    assert "received_at >=" in sql and "BIGINT" in sql  # 시각 조건이 조회 안에 있다(개수 상한 전에 거름)
    assert "status" not in sql  # 대기·실패 TRANSFER도 거부 근거
    assert await processing._transfer_involving(_Capture(scalar=None), owner, LIVE_TRIAL) is False
    assert await processing._transfer_involving(_Capture(scalar=None), owner, None) is True  # 시각 모르면 막음


async def test_chain_transaction_ids_include_snapshot_and_inbox_transactions():
    session = _Capture(rows=["inbox-renewal-tx", "chain-otx"])
    target = processing.Target(None, {}, [{"id": "snapshot-tx"}, {"id": "renew-tx"}], {})
    ids = await processing._chain_transaction_ids(session, {"transaction_id": "renew-tx",
                                                            "original_transaction_id": "chain-otx"}, target)
    assert ids == ["chain-otx", "inbox-renewal-tx", "renew-tx", "snapshot-tx"]


def test_apply_passes_the_inbox_receipt_time_to_finance():
    import inspect
    source = inspect.getsource(processing.apply)
    assert 'received_at=getattr(row, "received_at", None)' in source
    assert source.index("_ownership_lock") < source.index("await _finance(")


async def test_chain_conflict_checks_other_users_subscriptions_and_payments():
    owner = uuid.uuid4()
    session = _Capture(scalar=None)
    assert await processing._chain_conflict(session, owner, ["chain-otx", "renew-tx"]) is False
    subs_sql, pay_sql = (_sql(s) for s in session.statements)
    for fragment in ("subscriptions.user_id !=", "original_transaction_id IN", "latest_transaction_id IN",
                     "access_evidence"):
        assert fragment in subs_sql
    assert "payments.user_id !=" in pay_sql and "store_transaction_id IN" in pay_sql
    assert await processing._chain_conflict(_Capture(scalar=uuid.uuid4()), owner, ["chain-otx"]) is True


async def test_chain_origin_query_requires_first_transaction_of_the_chain():
    session = _Capture(rows=[])
    await processing._chain_origin_rows(session, {"original_transaction_id": "chain-otx"})
    sql = _sql(session.statements[0])
    assert "INITIAL_PURCHASE" in sql and sql.count("'chain-otx'") == 2
