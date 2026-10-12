"""기존 지급으로 추가 지급이 불가능할 때만 외부 보너스 심사를 생략한다."""
import uuid
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from app.services import subscription
from tests.test_subscription import FakeSession


@pytest.fixture
def context(monkeypatch):
    uid = uuid.uuid4()
    pay = SimpleNamespace(user_id=uid, subscription_plan="monthly", subscription_hay_grant_id=None,
                          subscription_bonus_review="held:review_pending")
    sub = SimpleNamespace(user_id=uid)
    review = AsyncMock(return_value="held:provider_history_unavailable")
    grant = AsyncMock()
    ledger = AsyncMock()
    monkeypatch.setattr(subscription, "_bonus_review", review)
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    monkeypatch.setattr(subscription.hay_ledger, "apply", ledger)
    return pay, sub, review, grant, ledger


@pytest.mark.parametrize("revoked", [None, "synthetic-revoked"])
async def test_existing_lifetime_grant_ends_pending_without_new_financial_effect(context, revoked):
    pay, sub, review, grant, ledger = context
    original = SimpleNamespace(id=uuid.uuid4(), user_id=pay.user_id, plan="monthly", revoked_at=revoked)
    session = FakeSession(exec_results=[[original]])
    result = await subscription._finish_subscription_bonus(session, sub, pay, {}, "monthly")
    assert result.outcome == subscription.HANDLED
    assert pay.subscription_bonus_review == "not_granted:user_plan_previously_granted"
    assert pay.subscription_hay_grant_id is None and pay.user_id == original.user_id
    assert original.revoked_at == revoked and session.added == []
    review.assert_not_awaited()
    grant.assert_not_awaited()
    ledger.assert_not_awaited()


@pytest.mark.parametrize("reason,linked", [("granted", False), ("granted:refund_reversed", False),
                                         ("granted", True), (None, True)])
async def test_existing_payment_link_and_reason_are_immutable(context, reason, linked):
    pay, sub, review, grant, ledger = context
    pay.subscription_bonus_review = reason
    pay.subscription_hay_grant_id = uuid.uuid4() if linked else None
    before = vars(pay).copy()
    result = await subscription._finish_subscription_bonus(FakeSession(), sub, pay, {}, "monthly")
    assert result.outcome == subscription.HANDLED and vars(pay) == before
    review.assert_not_awaited()
    grant.assert_not_awaited()
    ledger.assert_not_awaited()


@pytest.mark.parametrize("variation", ["different_owner", "different_plan", "unsupported_plan", "absent_grant"])
async def test_shortcut_cannot_use_unrelated_or_absent_grant(context, monkeypatch, variation):
    pay, sub, review, grant, ledger = context
    exists = AsyncMock(return_value=variation != "absent_grant")
    monkeypatch.setattr(subscription, "_grant_exists", exists)
    plan = "monthly"
    if variation == "different_owner":
        sub.user_id = uuid.uuid4()
    elif variation == "different_plan":
        pay.subscription_plan = "yearly"
    elif variation == "unsupported_plan":
        pay.subscription_plan = plan = "unsupported"
    result = await subscription._finish_subscription_bonus(FakeSession(), sub, pay, {}, plan)
    assert result.outcome == subscription.HANDLED_PENDING
    assert pay.subscription_hay_grant_id is None and pay.subscription_bonus_review == "held:provider_history_unavailable"
    review.assert_awaited_once()
    grant.assert_not_awaited()
    ledger.assert_not_awaited()
    if variation == "absent_grant":
        exists.assert_awaited_once_with(ANY, pay.user_id, "monthly")
    else:
        exists.assert_not_awaited()
