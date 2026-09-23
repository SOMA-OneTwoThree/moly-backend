"""Deleted payer history never fabricates money or overrides provider current access."""
from unittest.mock import AsyncMock

import pytest

from app.services import subscription
from tests.test_subscription import FakeSession, _rc_event
from tests.test_subscription_transfer import reconciliation_fixture


@pytest.mark.parametrize('event_type', ['CANCELLATION', 'REFUND_REVERSED'])
@pytest.mark.parametrize('current_access', [False, True])
async def test_missing_original_payment_syncs_only_current_provider_access(monkeypatch, event_type, current_access):
    session, snapshot, transactions, now = reconciliation_fixture(gives_access=current_access)
    monkeypatch.setattr(subscription, '_payment_by_tx', AsyncMock(return_value=None))
    monkeypatch.setattr(subscription, '_by_original_tx', AsyncMock(return_value=session.chain))
    monkeypatch.setattr(subscription, '_snapshot_for_transaction', AsyncMock(return_value=(snapshot, transactions)))
    ledger = AsyncMock()
    monkeypatch.setattr(subscription.hay_ledger, 'apply', ledger)
    result = await subscription.handle_revenuecat_event(session, _rc_event(
        type=event_type, cancel_reason='CUSTOMER_SUPPORT', original_transaction_id='original-store-tx',
        transaction_id='original-store-tx', app_user_id=snapshot['original_customer_id'],
        environment='SANDBOX', event_timestamp_ms=int(now.timestamp()*1000)))
    assert result.outcome == subscription.HANDLED_PENDING
    assert session.chain.status == ('active' if current_access else 'expired')
    assert str(session.chain.user_id) == snapshot['customer_id']
    assert session.added == []
    ledger.assert_not_awaited()


async def test_refund_before_any_subscription_does_not_create_restore_row(monkeypatch):
    monkeypatch.setattr(subscription, '_payment_by_tx', AsyncMock(return_value=None))
    monkeypatch.setattr(subscription, '_by_original_tx', AsyncMock(return_value=None))
    provider = AsyncMock()
    monkeypatch.setattr(subscription, '_snapshot_for_transaction', provider)
    session = FakeSession()
    result = await subscription.handle_revenuecat_event(session, _rc_event(type='CANCELLATION', cancel_reason='CUSTOMER_SUPPORT'))
    assert result.outcome == subscription.DEPENDENCY_MISSING
    assert session.added == []
    provider.assert_not_awaited()


async def test_unrelated_provider_chain_never_changes_access(monkeypatch):
    session, snapshot, transactions, now = reconciliation_fixture(gives_access=False)
    snapshot['id'] = 'unrelated'
    before = dict(vars(session.chain))
    monkeypatch.setattr(subscription, '_payment_by_tx', AsyncMock(return_value=None))
    monkeypatch.setattr(subscription, '_by_original_tx', AsyncMock(return_value=session.chain))
    monkeypatch.setattr(subscription, '_snapshot_for_transaction', AsyncMock(return_value=(snapshot, transactions)))
    result = await subscription.handle_revenuecat_event(session, _rc_event(type='CANCELLATION', cancel_reason='CUSTOMER_SUPPORT'))
    assert result.outcome == subscription.DEPENDENCY_MISSING
    assert vars(session.chain) == before and session.added == []
