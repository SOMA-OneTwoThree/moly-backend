"""A transferred subscription must not reassign a historical paid transaction."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import uuid

import pytest

from app.models.payment import Payment
from app.models.profile import Profile
from app.models.subscription import Subscription
from app.models.subscription_hay_grant import SubscriptionHayGrant
from app.services import hay_ledger, revenuecat, subscription


class ReadOnlySession:
    def add(self, value):
        raise AssertionError('A paid transaction replay must not insert another payment')


def existing_payment(*, subscription_id, user_id, status='paid'):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=user_id, order_id=None,
        subscription_id=subscription_id, store_transaction_id='historical-paid-tx',
        store='app_store', amount=Decimal('8.00'), currency='USD',
        status=status, subscription_plan='monthly', subscription_hay_grant_id=None,
    )


@pytest.mark.parametrize('status', ['paid', 'refunded'])
async def test_same_chain_replay_keeps_original_financial_owner(monkeypatch, status):
    original_owner, access_owner, sub_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    payment = existing_payment(subscription_id=sub_id, user_id=original_owner, status=status)
    before = vars(payment).copy()

    async def by_transaction(session, transaction_id):
        assert transaction_id == payment.store_transaction_id
        return payment

    monkeypatch.setattr(subscription, '_payment_by_tx', by_transaction)
    await subscription._record_subscription_payment(
        ReadOnlySession(), SimpleNamespace(id=sub_id, user_id=access_owner, plan='yearly'),
        {'transaction_id': payment.store_transaction_id, 'app_user_id': str(access_owner)},
    )
    assert vars(payment) == before


async def test_transfer_does_not_allow_transaction_reuse_by_another_subscription(monkeypatch):
    user_id = uuid.uuid4()
    payment = existing_payment(subscription_id=uuid.uuid4(), user_id=user_id)
    before = vars(payment).copy()

    async def by_transaction(session, transaction_id):
        return payment

    monkeypatch.setattr(subscription, '_payment_by_tx', by_transaction)
    with pytest.raises(subscription._HandlerSignal) as error:
        await subscription._record_subscription_payment(
            ReadOnlySession(), SimpleNamespace(id=uuid.uuid4(), user_id=user_id, plan='monthly'),
            {'transaction_id': payment.store_transaction_id},
        )
    assert error.value.outcome == subscription.PERMANENT_FAILURE
    assert vars(payment) == before


async def test_transfer_does_not_reclassify_consumable_transaction(monkeypatch):
    user_id, sub_id = uuid.uuid4(), uuid.uuid4()
    payment = existing_payment(subscription_id=None, user_id=user_id)
    payment.order_id = uuid.uuid4()
    before = vars(payment).copy()

    async def by_transaction(session, transaction_id):
        return payment

    monkeypatch.setattr(subscription, '_payment_by_tx', by_transaction)
    with pytest.raises(subscription._HandlerSignal) as error:
        await subscription._record_subscription_payment(
            ReadOnlySession(), SimpleNamespace(id=sub_id, user_id=user_id, plan='monthly'),
            {'transaction_id': payment.store_transaction_id},
        )
    assert error.value.outcome == subscription.PERMANENT_FAILURE
    assert vars(payment) == before


# The reconciliation tests exercise the server's current RevenueCat snapshot,
# not the stale destination embedded in an earlier TRANSFER webhook.
class Rows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.values[0] if self.values else None

    def scalar_one_or_none(self):
        assert len(self.values) <= 1
        return self.first()

    def __iter__(self):
        return iter(self.values)


class ReconciliationSession:
    def __init__(self, chain, owner):
        self.chain = chain
        self.owner = owner
        self.added = []

    async def execute(self, statement):
        entity = statement.column_descriptions[0]['entity']
        if entity is Subscription:
            return Rows([self.chain])
        if entity is Profile:
            return Rows([self.owner] if self.owner else [])
        if entity is Payment:
            return Rows([])
        raise AssertionError(f'Unexpected reconciliation query: {entity}')

    async def get(self, model, key, **kwargs):
        if model is Profile:
            return self.owner if self.owner and self.owner.id == key else None
        if model is Subscription:
            return self.chain if self.chain.id == key else None
        return None

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        pass


def reconciliation_fixture(*, gives_access=True, status='active'):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old_owner, current_owner = uuid.uuid4(), uuid.uuid4()
    chain = SimpleNamespace(
        id=uuid.uuid4(), user_id=old_owner, plan='monthly', status='active',
        original_transaction_id='original-store-tx', latest_transaction_id='latest-store-tx',
        rc_subscription_id='rc-chain', ownership_changed_at=None,
        purchased_at=now-timedelta(days=30), expires_at=now+timedelta(days=1),
        last_event_at=now-timedelta(days=1), environment='SANDBOX',
        auto_renew_enabled=True, store_trial_ends_at=None,
    )
    end = now + timedelta(days=15)
    snapshot = {
        'id': 'rc-chain', 'customer_id': str(current_owner),
        'original_customer_id': str(old_owner), 'product_id': 'rc-product-monthly',
        'store': 'app_store', 'environment': 'sandbox',
        'store_subscription_identifier': 'latest-store-tx',
        'starts_at': int((now-timedelta(days=30)).timestamp()*1000),
        'current_period_starts_at': int(now.timestamp()*1000),
        'current_period_ends_at': int(end.timestamp()*1000), 'ends_at': int(end.timestamp()*1000),
        'gives_access': gives_access, 'status': status, 'ownership': 'purchased',
        'pending_payment': False, 'auto_renewal_status': 'will_not_renew',
        'entitlements': {'items': [{'lookup_key': 'pro'}], 'next_page': None},
    }
    transactions = [{
        'id': 'original-store-tx',
        'product_store_identifier': 'com.geniusjun.moly.plus.monthly',
        'purchased_at': snapshot['starts_at'],
        'revenue_in_local_currency': {'gross': 8, 'currency': 'USD'},
    }, {
        'id': 'latest-store-tx',
        'product_store_identifier': 'com.geniusjun.moly.plus.monthly',
        'purchased_at': snapshot['current_period_starts_at'],
        'revenue_in_local_currency': {'gross': 8, 'currency': 'USD'},
    }]
    owner = SimpleNamespace(id=current_owner, created_at=now-timedelta(days=60))
    return ReconciliationSession(chain, owner), snapshot, transactions, now


async def test_reconciliation_changes_access_without_payment_or_hay(monkeypatch):
    session, snapshot, transactions, now = reconciliation_fixture()

    async def forbid_grant(*args, **kwargs):
        raise AssertionError('TRANSFER must not issue or move hay')

    monkeypatch.setattr(hay_ledger, 'apply', forbid_grant)
    await subscription._reconcile_subscription(session, snapshot, transactions, now)
    assert session.chain.user_id == session.owner.id
    assert session.chain.status == 'active'
    assert session.chain.auto_renew_enabled is False
    assert session.chain.expires_at == datetime.fromtimestamp(snapshot['ends_at']/1000, timezone.utc)
    assert not any(isinstance(row, Payment) for row in session.added)


async def test_reconciliation_uses_gives_access_even_when_status_is_active(monkeypatch):
    session, snapshot, transactions, now = reconciliation_fixture(gives_access=False)

    async def forbid_grant(*args, **kwargs):
        raise AssertionError('An inaccessible subscription must never grant hay')

    monkeypatch.setattr(hay_ledger, 'apply', forbid_grant)
    await subscription._reconcile_subscription(session, snapshot, transactions, now)
    assert session.chain.status not in ('active', 'grace_period')
    assert not any(isinstance(row, Payment) for row in session.added)


async def test_reconciliation_does_not_recreate_deleted_destination(monkeypatch):
    session, snapshot, transactions, now = reconciliation_fixture()
    session.owner = None

    async def forbid_grant(*args, **kwargs):
        raise AssertionError('A deleted destination must not receive hay')

    monkeypatch.setattr(hay_ledger, 'apply', forbid_grant)
    await subscription._reconcile_subscription(session, snapshot, transactions, now)
    assert session.chain.user_id is None
    assert not any(isinstance(row, (Profile, Payment)) for row in session.added)


async def test_replayed_paid_event_does_not_grant_to_new_access_owner(monkeypatch):
    session, snapshot, transactions, now = reconciliation_fixture()
    original_owner = session.chain.user_id
    session.chain.user_id = session.owner.id
    historical_payment = existing_payment(
        subscription_id=session.chain.id, user_id=original_owner,
    )
    before = vars(historical_payment).copy()

    async def by_original_transaction(*args, **kwargs):
        return session.chain

    async def by_transaction(*args, **kwargs):
        return historical_payment

    async def forbid_grant(*args, **kwargs):
        raise AssertionError('Replaying an old paid event must not reward the new account')

    monkeypatch.setattr(subscription, '_by_original_tx', by_original_transaction)
    monkeypatch.setattr(subscription, '_payment_by_tx', by_transaction)
    monkeypatch.setattr(subscription, '_grant_if_first', forbid_grant)
    monkeypatch.setattr(hay_ledger, 'apply', forbid_grant)
    result = await subscription._handle_active(session, 'RENEWAL', {
        'type': 'RENEWAL', 'app_user_id': str(session.owner.id),
        'product_id': 'com.geniusjun.moly.plus.monthly',
        'original_transaction_id': session.chain.original_transaction_id,
        'transaction_id': historical_payment.store_transaction_id,
        'period_type': 'NORMAL', 'store': 'APP_STORE', 'environment': 'SANDBOX',
        'price_in_purchased_currency': 8, 'currency': 'USD',
        'purchased_at_ms': int((now-timedelta(days=30)).timestamp()*1000),
        'expiration_at_ms': int(now.timestamp()*1000),
    }, session.owner.id, now-timedelta(days=30))
    assert result.outcome in (subscription.HANDLED, subscription.NO_OP)
    assert vars(historical_payment) == before
    assert session.chain.user_id == session.owner.id
    assert not any(isinstance(row, Payment) for row in session.added)


@pytest.mark.parametrize('invalid_field,invalid_value', [
    ('customer_id', ''),
    ('gives_access', 'true'),
    ('environment', 'production'),
    ('store', 'promotional'),
])
async def test_unverified_snapshot_does_not_replace_existing_owner(
    monkeypatch, invalid_field, invalid_value,
):
    session, snapshot, transactions, now = reconciliation_fixture()
    before = vars(session.chain).copy()
    snapshot[invalid_field] = invalid_value
    with pytest.raises(subscription._HandlerSignal):
        await subscription._reconcile_subscription(session, snapshot, transactions, now)
    assert vars(session.chain) == before
    assert not session.added


async def test_late_transfer_reconciles_current_third_owner_and_replay_does_not_move_money(monkeypatch):
    session, current, transactions, now = reconciliation_fixture()
    original_owner, intermediate_owner = session.chain.user_id, uuid.uuid4()
    stale_overview = dict(current, customer_id=str(intermediate_owner))

    async def no_lock(session):
        pass

    async def list_customer(customer_id):
        assert customer_id in (str(original_owner), str(intermediate_owner))
        return [stale_overview]

    async def search(transaction_id):
        assert transaction_id == session.chain.original_transaction_id
        return [stale_overview]

    async def get_current(rc_id):
        assert rc_id == current['id']
        return current

    async def get_transactions(rc_id):
        return transactions

    async def forbid_hay(*args, **kwargs):
        raise AssertionError('A transfer replay must not mutate either account balance')

    monkeypatch.setattr(subscription, '_ownership_lock', no_lock)
    monkeypatch.setattr(revenuecat, 'customer_subscriptions', list_customer)
    monkeypatch.setattr(revenuecat, 'search_subscriptions', search)
    monkeypatch.setattr(revenuecat, 'get_subscription', get_current)
    monkeypatch.setattr(revenuecat, 'subscription_transactions', get_transactions)
    monkeypatch.setattr(hay_ledger, 'apply', forbid_hay)
    event = {
        'type': 'TRANSFER', 'transferred_from': ['$RCAnonymousID:alias', str(original_owner)],
        'transferred_to': [str(intermediate_owner)],
        'event_timestamp_ms': int((now-timedelta(days=2)).timestamp()*1000),
    }
    for _ in range(2):
        result = await subscription.handle_revenuecat_event(session, event)
        assert result.outcome == subscription.HANDLED
        assert session.chain.user_id == session.owner.id
        assert session.chain.last_event_at == now-timedelta(days=1)
    assert not any(isinstance(row, Payment) for row in session.added)


@pytest.mark.parametrize('failure_at', ['customer', 'search', 'subscription', 'transactions'])
async def test_provider_failure_does_not_change_access_or_finances(monkeypatch, failure_at):
    session, snapshot, transactions, now = reconciliation_fixture()
    before = vars(session.chain).copy()

    async def no_lock(session):
        pass

    async def list_customer(customer_id):
        if failure_at == 'customer':
            raise revenuecat.RevenueCatError('RevenueCat read HTTP 429')
        return [snapshot]

    async def search(transaction_id):
        if failure_at == 'search':
            raise revenuecat.RevenueCatError('RevenueCat read HTTP 503')
        return [snapshot]

    async def get_current(rc_id):
        if failure_at == 'subscription':
            raise revenuecat.RevenueCatError('RevenueCat read HTTP 404')
        return snapshot

    async def get_transactions(rc_id):
        if failure_at == 'transactions':
            raise revenuecat.RevenueCatError('RevenueCat list incomplete')
        return transactions

    async def forbid_hay(*args, **kwargs):
        raise AssertionError('An unverified transfer must not change hay')

    monkeypatch.setattr(subscription, '_ownership_lock', no_lock)
    monkeypatch.setattr(revenuecat, 'customer_subscriptions', list_customer)
    monkeypatch.setattr(revenuecat, 'search_subscriptions', search)
    monkeypatch.setattr(revenuecat, 'get_subscription', get_current)
    monkeypatch.setattr(revenuecat, 'subscription_transactions', get_transactions)
    monkeypatch.setattr(hay_ledger, 'apply', forbid_hay)
    result = await subscription.handle_revenuecat_event(session, {
        'type': 'TRANSFER', 'transferred_from': [str(session.chain.user_id)],
        'transferred_to': [str(session.owner.id)], 'event_timestamp_ms': int(now.timestamp()*1000),
    })
    assert result.outcome == subscription.DEPENDENCY_MISSING
    assert vars(session.chain) == before
    assert not session.added


async def test_provider_grace_uses_only_existing_verified_deadline(monkeypatch):
    session, snapshot, transactions, now = reconciliation_fixture(status='in_grace_period')
    session.chain.status = 'grace_period'
    old_deadline = session.chain.expires_at
    snapshot['current_period_ends_at'] = snapshot['ends_at'] = int((now-timedelta(days=1)).timestamp()*1000)
    await subscription._reconcile_subscription(session, snapshot, transactions, now)
    assert session.chain.status == 'grace_period'
    assert session.chain.expires_at == old_deadline
    assert session.chain.user_id == session.owner.id


async def test_provider_grace_without_verified_deadline_does_not_invent_access(monkeypatch):
    session, snapshot, transactions, now = reconciliation_fixture(status='in_grace_period')
    snapshot['current_period_ends_at'] = snapshot['ends_at'] = int((now-timedelta(days=1)).timestamp()*1000)
    before = vars(session.chain).copy()
    with pytest.raises(subscription._HandlerSignal):
        await subscription._reconcile_subscription(session, snapshot, transactions, now)
    assert vars(session.chain) == before


async def test_transfer_of_store_trial_preserves_trial_end_without_a_bonus(monkeypatch):
    session, snapshot, transactions, now = reconciliation_fixture(status='trialing')
    for tx in transactions:
        tx['revenue_in_local_currency']['gross'] = 0

    async def forbid_hay(*args, **kwargs):
        raise AssertionError('A transferred free trial is not a first paid purchase')

    monkeypatch.setattr(hay_ledger, 'apply', forbid_hay)
    await subscription._reconcile_subscription(session, snapshot, transactions, now)
    assert session.chain.user_id == session.owner.id
    assert session.chain.store_trial_ends_at == session.chain.expires_at
    assert not any(isinstance(row, Payment) for row in session.added)


async def test_verified_anonymous_owner_removes_previous_app_account_access():
    session, snapshot, transactions, now = reconciliation_fixture()
    snapshot['customer_id'] = '$RCAnonymousID:restored-anonymously'
    await subscription._reconcile_subscription(session, snapshot, transactions, now)
    assert session.chain.user_id is None
    assert not any(isinstance(row, (Profile, Payment)) for row in session.added)


@pytest.mark.parametrize('prior_attempts,expected_status', [(0, 'pending'), (4, 'failed')])
async def test_bonus_hold_commits_verified_access_and_payment_with_bounded_retry(
    monkeypatch, prior_attempts, expected_status,
):
    row = SimpleNamespace(
        payload={'type': 'RENEWAL'}, status='pending', attempts=prior_attempts,
        last_error=None, processed_at=None, next_attempt_at=None,
    )
    calls = []

    class InboxSession:
        async def execute(self, statement):
            return Rows([row])

        async def commit(self):
            calls.append('commit')

        async def rollback(self):
            calls.append('rollback')

    async def verified_but_bonus_held(session, event):
        return subscription.HandlerResult(
            subscription.HANDLED_PENDING, 'held:provider_history_unavailable',
        )

    monkeypatch.setattr(subscription, 'handle_revenuecat_event', verified_but_bonus_held)
    before = datetime.now(timezone.utc)
    result = await subscription.process_event(InboxSession(), 'paid-bonus-retry')
    assert result == subscription.HANDLED_PENDING
    assert calls == ['commit']
    assert row.status == expected_status
    assert row.attempts == prior_attempts + 1
    assert row.last_error == 'held:provider_history_unavailable'
    assert row.next_attempt_at > before
    assert (row.processed_at is not None) == (expected_status == 'failed')


async def test_provider_error_rolls_back_partial_changes_before_recording_retry(monkeypatch):
    row = SimpleNamespace(payload={'type': 'TRANSFER'}, status='pending', attempts=0)
    calls = []

    class InboxSession:
        async def execute(self, statement):
            return Rows([row])

        async def commit(self):
            calls.append('commit')

        async def rollback(self):
            calls.append('rollback')

    async def provider_error_after_first_chain(session, event):
        calls.append('first_chain_changed')
        raise revenuecat.RevenueCatError('RevenueCat read HTTP 503')

    async def record_retry_after_rollback(session, event_id, outcome, reason):
        assert calls == ['first_chain_changed', 'rollback']
        assert event_id == 'multi-chain-transfer'
        assert outcome == subscription.DEPENDENCY_MISSING
        assert reason == 'RevenueCat read HTTP 503'
        calls.append('retry_recorded')

    monkeypatch.setattr(subscription, '_dispatch', provider_error_after_first_chain)
    monkeypatch.setattr(subscription, '_record_terminal', record_retry_after_rollback)
    result = await subscription.process_event(InboxSession(), 'multi-chain-transfer')
    assert result == subscription.DEPENDENCY_MISSING
    assert calls == ['first_chain_changed', 'rollback', 'retry_recorded']


async def test_past_owner_refund_reversal_preserves_current_access_state(monkeypatch):
    session, snapshot, transactions, now = reconciliation_fixture()
    original_owner = session.chain.user_id
    session.chain.user_id = session.owner.id
    session.chain.plan = 'yearly'
    before = vars(session.chain).copy()
    payment = existing_payment(
        subscription_id=session.chain.id, user_id=original_owner, status='refunded',
    )
    payment.subscription_bonus_review = 'granted'
    payment.subscription_hay_grant_id = uuid.uuid4()
    grant = SimpleNamespace(
        id=payment.subscription_hay_grant_id, user_id=original_owner, plan='monthly',
        revoked_at=now-timedelta(days=1), clawback_hay_transaction_id=99,
    )
    financial_changes = []

    async def get_grant(statement):
        assert statement.column_descriptions[0]['entity'] is SubscriptionHayGrant
        return Rows([grant])

    async def actual_original_amount(db_session, linked_grant):
        assert linked_grant is grant
        return 1234

    async def apply_to_original_owner(db_session, user_id, kind, amount, **kwargs):
        financial_changes.append((user_id, kind, amount))

    monkeypatch.setattr(session, 'execute', get_grant)
    monkeypatch.setattr(subscription, '_grant_ledger_amount', actual_original_amount)
    monkeypatch.setattr(hay_ledger, 'apply', apply_to_original_owner)
    result = await subscription._reverse_subscription_refund(session, payment, now)
    assert result.outcome == subscription.HANDLED
    assert financial_changes == [(original_owner, 'admin_adjustment', 1234)]
    assert payment.status == 'paid'
    assert grant.revoked_at is None
    assert vars(session.chain) == before
