"""Exact guarded old-purchaser free-period refund after a verified transfer."""
import uuid
from unittest.mock import AsyncMock

import pytest

from app.services import subscription
from tests.test_subscription import FakeSession, UID, _rc_event, _sub


@pytest.mark.parametrize('snapshot_owner,original_owner,expected', [
    ('current', 'purchaser', subscription.HANDLED),
    ('unrelated', 'purchaser', subscription.DEPENDENCY_MISSING),
    ('current', 'unrelated', subscription.DEPENDENCY_MISSING),
])
async def test_free_refund_requires_provider_verified_current_and_original_owner(monkeypatch, snapshot_owner, original_owner, expected):
    current = uuid.uuid4()
    sub = _sub(user_id=current, original_transaction_id='o-rc-1', latest_transaction_id='t-rc-1',
               rc_subscription_id='rc-sub', environment='SANDBOX')
    snapshot = {'id': 'rc-sub', 'customer_id': str(current) if snapshot_owner == 'current' else str(uuid.uuid4()),
                'original_customer_id': UID if original_owner == 'purchaser' else str(uuid.uuid4()),
                'store_subscription_identifier': 't-rc-1'}
    monkeypatch.setattr(subscription, '_by_original_tx', AsyncMock(return_value=sub))
    monkeypatch.setattr(subscription, '_snapshot_for_transaction', AsyncMock(return_value=(snapshot, [{'id': 't-rc-1'}])))
    result = await subscription.handle_revenuecat_event(FakeSession(), _rc_event(
        type='CANCELLATION', cancel_reason='CUSTOMER_SUPPORT', period_type='TRIAL',
        price_in_purchased_currency=0, environment='SANDBOX'))
    assert result.outcome == expected
    assert sub.user_id == current
    assert sub.status == ('revoked' if expected == subscription.HANDLED else 'active')
