"""Store-period regressions using synthetic, fixed-time RevenueCat fixtures.

These tests never open a database or call a provider. Assertions describe access
outcomes, including observations made without another webhook at a period boundary.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.subscription_access import (
    EvidenceError,
    build_evidence,
    project,
    resolve_access,
    validate,
)

UTC = timezone.utc
START = datetime(2026, 10, 29, 14, 37, tzinfo=UTC)
BOUNDARY = START + timedelta(days=3)
END = BOUNDARY + timedelta(days=365)
OBSERVED = BOUNDARY - timedelta(hours=8)
OWNER_A = "11111111-1111-4111-8111-111111111111"
OWNER_B = "22222222-2222-4222-8222-222222222222"
MONTHLY = "com.geniusjun.moly.plus.monthly"
YEARLY = "com.geniusjun.moly.plus.yearly"
PRODUCTS = {MONTHLY: "monthly", YEARLY: "yearly"}


def ms(value):
    return int(value.timestamp() * 1000)


def prepaid_fixture():
    snapshot = {
        "id": "rc-current", "customer_id": OWNER_A, "original_customer_id": OWNER_A,
        "store": "app_store", "environment": "sandbox",
        "store_subscription_identifier": "store-trial", "status": "trialing",
        "gives_access": True, "pending_payment": False,
        "auto_renewal_status": "has_already_renewed",
        "current_period_starts_at": ms(START), "current_period_ends_at": ms(BOUNDARY),
        "ends_at": ms(END),
        "pending_changes": {
            "store_subscription_identifier": "store-paid", "status": "active",
            "current_period_starts_at": ms(BOUNDARY), "current_period_ends_at": ms(END),
            "gives_access": True, "pending_payment": False,
        },
    }
    transactions = [
        {"id": "store-trial", "purchased_at": ms(START),
         "effective_expiration_date": ms(BOUNDARY), "product_store_identifier": MONTHLY,
         "revenue_in_local_currency": {"gross": 0}},
        {"id": "store-paid", "purchased_at": ms(BOUNDARY),
         "effective_expiration_date": ms(END), "product_store_identifier": YEARLY,
         "revenue_in_local_currency": {"gross": 5500}},
    ]
    events = [
        {"type": "INITIAL_PURCHASE", "transaction_id": "store-trial",
         "environment": "SANDBOX", "app_user_id": OWNER_A, "period_type": "TRIAL",
         "price_in_purchased_currency": 0, "purchased_at_ms": ms(START),
         "expiration_at_ms": ms(BOUNDARY)},
        {"type": "RENEWAL", "transaction_id": "store-paid", "environment": "SANDBOX",
         "app_user_id": OWNER_A, "period_type": "NORMAL", "price_in_purchased_currency": 5500,
         "purchased_at_ms": ms(BOUNDARY), "expiration_at_ms": ms(END)},
    ]
    return snapshot, transactions, events


def evidence(*, now=OBSERVED):
    return build_evidence(*prepaid_fixture(), PRODUCTS, now=now)


def row(proof=None, *, owner=OWNER_A):
    return SimpleNamespace(
        id="synthetic-subscription", user_id=owner, access_evidence=proof,
        status="active", plan="monthly", expires_at=END, purchased_at=START,
        store_trial_ends_at=END, auto_renew_enabled=True,
        latest_transaction_id="store-trial", rc_subscription_id="rc-current",
        environment="SANDBOX",
    )


def paid_snapshot(*, owner=OWNER_A, rc_id="rc-current", now=BOUNDARY + timedelta(days=1)):
    snapshot, transactions, events = prepaid_fixture()
    snapshot.update({
        "id": rc_id, "customer_id": owner, "status": "active",
        "store_subscription_identifier": "store-paid",
        "current_period_starts_at": ms(BOUNDARY), "current_period_ends_at": ms(END),
        "pending_changes": None,
    })
    return snapshot, transactions, events, now


def test_prepaid_renewal_does_not_extend_current_trial():
    sub = row(evidence())
    access = resolve_access(sub, OBSERVED)
    assert access.plan == "monthly"
    assert access.store_trial_ends_at == BOUNDARY
    assert access.expires_at == BOUNDARY
    assert access.purchased_at == START


@pytest.mark.parametrize("offset", [timedelta(milliseconds=-1), timedelta(0), timedelta(milliseconds=1)])
def test_boundary_switches_to_paid_without_another_webhook(offset):
    sub = row(evidence())
    access = resolve_access(sub, BOUNDARY + offset)
    assert access is not None
    if offset < timedelta(0):
        assert access.plan == "monthly" and access.store_trial_ends_at == BOUNDARY
    else:
        assert access.plan == "yearly" and access.store_trial_ends_at is None
        assert access.purchased_at == BOUNDARY and access.expires_at == END
    assert resolve_access(sub, END) is None


def test_future_expiry_and_will_renew_alone_do_not_create_paid_access():
    snapshot, transactions, events = prepaid_fixture()
    snapshot["auto_renewal_status"] = "will_renew"
    snapshot["pending_changes"] = None
    proof = build_evidence(snapshot, transactions[:1], events[:1], PRODUCTS, now=OBSERVED)
    assert resolve_access(row(proof), BOUNDARY) is None


@pytest.mark.parametrize("changes", [
    {"gives_access": False}, {"gives_access": None}, {"pending_payment": True},
])
def test_explicit_unverified_or_denied_next_period_is_not_granted(changes):
    snapshot, transactions, events = prepaid_fixture()
    snapshot["pending_changes"].update(changes)
    try:
        proof = build_evidence(snapshot, transactions, events, PRODUCTS, now=OBSERVED)
    except EvidenceError:
        return  # Keep the previous verified revision and request revalidation.
    assert resolve_access(row(proof), BOUNDARY) is None


def test_explicit_future_revocation_removes_previously_verified_next_period():
    previous = evidence()
    snapshot, transactions, events = prepaid_fixture()
    snapshot["pending_changes"]["gives_access"] = False
    updated = build_evidence(snapshot, transactions, events, PRODUCTS,
                             now=OBSERVED + timedelta(minutes=1), previous=previous)
    assert resolve_access(row(updated), OBSERVED) is not None
    assert resolve_access(row(updated), BOUNDARY) is None


def test_refund_shortens_its_period_without_erasing_valid_past_access():
    snapshot, transactions, events, refund_at = paid_snapshot(now=BOUNDARY + timedelta(days=1))
    snapshot.update({"gives_access": False, "status": "expired",
                     "current_period_ends_at": ms(refund_at), "ends_at": ms(refund_at)})
    proof = build_evidence(snapshot, transactions, events, PRODUCTS,
                           now=refund_at, previous=evidence())
    sub = row(proof)
    assert resolve_access(sub, refund_at - timedelta(milliseconds=1)) is not None
    assert resolve_access(sub, refund_at) is None
    assert resolve_access(sub, END - timedelta(seconds=1)) is None


def test_false_current_access_wins_over_a_future_compatible_expiry():
    snapshot, transactions, events, now = paid_snapshot()
    snapshot.update({"gives_access": False, "status": "expired"})
    proof = build_evidence(snapshot, transactions, events, PRODUCTS, now=now)
    assert resolve_access(row(proof), now) is None


def test_refunded_zero_revenue_paid_period_is_never_classified_as_trial():
    snapshot, transactions, events, now = paid_snapshot()
    snapshot.update({"gives_access": False, "status": "expired"})
    transactions[1]["revenue_in_local_currency"]["gross"] = 0
    proof = build_evidence(snapshot, transactions, events, PRODUCTS, now=now)
    paid = [p for p in proof["periods"] if p["transaction_id"] == "store-paid"]
    assert all(p["kind"] != "trial" for p in paid)
    assert resolve_access(row(proof), now) is None


def test_refund_does_not_erase_previously_proven_paid_history_when_gross_becomes_zero():
    previous = evidence()
    snapshot, transactions, events, refund_at = paid_snapshot()
    snapshot.update({"gives_access": False, "status": "expired",
                     "current_period_ends_at": ms(refund_at), "ends_at": ms(refund_at)})
    transactions[1]["revenue_in_local_currency"]["gross"] = 0
    # The prior verified revision already proves payment, even if the original
    # purchase webhook is unavailable in this revalidation's supplied history.
    updated = build_evidence(snapshot, transactions, events[:1], PRODUCTS,
                             now=refund_at, previous=previous)
    assert resolve_access(row(updated), refund_at - timedelta(milliseconds=1)) is not None
    assert resolve_access(row(updated), refund_at) is None


def test_different_rc_object_cannot_overwrite_current_subscription_evidence():
    current = evidence()
    snapshot = {
        "id": "rc-old", "customer_id": OWNER_A, "store": "app_store",
        "environment": "sandbox", "store_subscription_identifier": "old-paid",
        "status": "expired", "gives_access": False,
        "auto_renewal_status": "will_not_renew",
        "current_period_starts_at": ms(START - timedelta(days=60)),
        "current_period_ends_at": ms(START - timedelta(days=30)),
        "ends_at": ms(START - timedelta(days=30)),
    }
    transactions = [{
        "id": "old-paid", "purchased_at": ms(START - timedelta(days=60)),
        "effective_expiration_date": ms(START - timedelta(days=30)),
        "product_store_identifier": MONTHLY, "revenue_in_local_currency": {"gross": 8},
    }]
    before = deepcopy(current)
    with pytest.raises(EvidenceError):
        build_evidence(snapshot, transactions, [], PRODUCTS, now=OBSERVED, previous=current)
    assert current == before
    assert resolve_access(row(current), OBSERVED) is not None
    assert resolve_access(row(current), BOUNDARY).plan == "yearly"


def test_transfer_preserves_original_owners_past_and_bounds_new_owner():
    original = evidence()
    snapshot, transactions, events, transfer_at = paid_snapshot(owner=OWNER_B)
    updated = build_evidence(snapshot, transactions, events, PRODUCTS,
                             now=transfer_at, previous=original)
    sub = row(updated, owner=OWNER_B)
    assert resolve_access(sub, OBSERVED, OWNER_A) is not None
    assert resolve_access(sub, transfer_at, OWNER_A) is None
    assert resolve_access(sub, OBSERVED, OWNER_B) is None
    assert resolve_access(sub, transfer_at, OWNER_B) is not None


def test_round_trip_transfer_preserves_each_owners_actual_history():
    original = evidence()
    snapshot, transactions, events, first_transfer = paid_snapshot(owner=OWNER_B)
    owned_by_b = build_evidence(snapshot, transactions, events, PRODUCTS,
                                now=first_transfer, previous=original)
    second_transfer = first_transfer + timedelta(days=1)
    snapshot["customer_id"] = OWNER_A
    back_to_a = build_evidence(snapshot, transactions, events, PRODUCTS,
                               now=second_transfer, previous=owned_by_b)
    sub = row(back_to_a)
    assert resolve_access(sub, OBSERVED, OWNER_A) is not None
    assert resolve_access(sub, first_transfer + timedelta(hours=1), OWNER_A) is None
    assert resolve_access(sub, first_transfer + timedelta(hours=1), OWNER_B) is not None
    assert resolve_access(sub, second_transfer, OWNER_B) is None
    assert resolve_access(sub, second_transfer, OWNER_A) is not None


def test_same_owner_refresh_does_not_expand_unproven_past_ownership():
    original = evidence()
    assert resolve_access(row(original), START, OWNER_A) is None
    snapshot, transactions, events, now = paid_snapshot()
    updated = build_evidence(snapshot, transactions, events, PRODUCTS, now=now, previous=original)
    assert resolve_access(row(updated), START, OWNER_A) is None
    assert resolve_access(row(updated), OBSERVED, OWNER_A) is not None


def test_projection_keeps_correct_trial_and_verified_paid_horizon():
    proof = evidence()
    sub = row()
    project(sub, proof, OBSERVED)
    assert sub.store_trial_ends_at == BOUNDARY
    assert sub.expires_at == END
    assert sub.latest_transaction_id == "store-trial"
    assert sub.plan == "monthly" and sub.purchased_at == START
    # Reads must not modify ORM columns just because time passed.
    before = deepcopy(vars(sub))
    assert resolve_access(sub, BOUNDARY).plan == "yearly"
    assert vars(sub) == before


def test_projection_does_not_bridge_gap_between_verified_periods():
    snapshot, transactions, events = prepaid_fixture()
    gap_end = BOUNDARY + timedelta(days=2)
    transactions[1]["purchased_at"] = ms(gap_end)
    snapshot["pending_changes"]["current_period_starts_at"] = ms(gap_end)
    events[1]["purchased_at_ms"] = ms(gap_end)
    proof = build_evidence(snapshot, transactions, events, PRODUCTS, now=OBSERVED)
    sub = row()
    project(sub, proof, OBSERVED)
    assert sub.expires_at == BOUNDARY
    assert resolve_access(sub, BOUNDARY + timedelta(days=1)) is None
    assert resolve_access(sub, gap_end) is not None


def test_previous_json_is_not_mutated_while_building_or_projecting():
    previous = evidence()
    before = deepcopy(previous)
    snapshot, transactions, events, now = paid_snapshot(owner=OWNER_B)
    updated = build_evidence(snapshot, transactions, events, PRODUCTS, now=now, previous=previous)
    assert previous == before
    sub = row(owner=OWNER_B)
    project(sub, updated, now)
    assert sub.access_evidence is not updated
    assert sub.access_evidence["periods"] is not updated["periods"]


def test_missing_current_period_does_not_invent_access_or_overwrite_previous():
    previous = evidence()
    before = deepcopy(previous)
    snapshot, transactions, events = prepaid_fixture()
    snapshot["current_period_starts_at"] = None
    transactions[0]["purchased_at"] = None
    with pytest.raises(EvidenceError):
        build_evidence(snapshot, transactions, events, PRODUCTS, now=OBSERVED, previous=previous)
    assert previous == before


def test_evidence_validation_rejects_cross_environment_period():
    proof = evidence()
    proof["periods"][0]["environment"] = "PRODUCTION"
    with pytest.raises(EvidenceError):
        validate(proof)


def test_null_evidence_keeps_existing_legacy_entitlement_policy():
    sub = row()
    assert resolve_access(sub, OBSERVED) is sub
    assert resolve_access(sub, END) is None
    assert resolve_access(sub, OBSERVED, OWNER_B) is None


def grace_fixture():
    snapshot, transactions, events, _ = paid_snapshot()
    regular_end = BOUNDARY + timedelta(days=30)
    observed = regular_end + timedelta(hours=1)
    grace_end = regular_end + timedelta(days=3)
    snapshot.update({"status": "in_grace_period", "gives_access": True,
                     "pending_payment": True, "current_period_ends_at": ms(regular_end),
                     "ends_at": ms(regular_end), "auto_renewal_status": "will_not_renew"})
    transactions[1]["effective_expiration_date"] = ms(grace_end)
    return snapshot, transactions, events, observed, regular_end, grace_end


def test_current_transaction_effective_expiry_preserves_verified_grace_access():
    snapshot, transactions, events, observed, _, grace_end = grace_fixture()
    proof = build_evidence(snapshot, transactions, events, PRODUCTS, now=observed)
    sub = row(proof)
    access = resolve_access(sub, observed)
    assert access is not None and access.status == "grace_period"
    assert access.expires_at == grace_end and access.store_trial_ends_at is None
    assert resolve_access(sub, grace_end - timedelta(milliseconds=1)) is not None
    assert resolve_access(sub, grace_end) is None


def test_exact_current_transaction_billing_issue_supplies_verified_grace_deadline():
    snapshot, transactions, events, observed, regular_end, grace_end = grace_fixture()
    transactions[1]["effective_expiration_date"] = None
    events.append({"type": "BILLING_ISSUE", "transaction_id": "store-paid",
                   "environment": "SANDBOX", "app_user_id": OWNER_A,
                   "expiration_at_ms": ms(regular_end), "event_timestamp_ms": ms(observed),
                   "grace_period_expiration_at_ms": ms(grace_end)})
    proof = build_evidence(snapshot, transactions, events, PRODUCTS, now=observed)
    access = resolve_access(row(proof), observed)
    assert access is not None and access.status == "grace_period"
    assert access.expires_at == grace_end
    assert resolve_access(row(proof), grace_end) is None


@pytest.mark.parametrize("wrong_proof", [None, "wrong_transaction", "wrong_environment"])
def test_grace_without_matching_current_deadline_never_uses_future_next_period_end(wrong_proof):
    snapshot, transactions, events, observed, _, grace_end = grace_fixture()
    transactions[1]["effective_expiration_date"] = None
    snapshot.update({"auto_renewal_status": "has_already_renewed", "ends_at": ms(END),
                     "pending_changes": {"store_subscription_identifier": "unverified-next",
                                         "current_period_starts_at": ms(grace_end),
                                         "current_period_ends_at": ms(END),
                                         "gives_access": True, "pending_payment": False}})
    if wrong_proof:
        events.append({"type": "BILLING_ISSUE", "transaction_id": "store-paid",
                       "environment": "SANDBOX", "app_user_id": OWNER_A,
                       "event_timestamp_ms": ms(observed),
                       "grace_period_expiration_at_ms": ms(grace_end)})
        if wrong_proof == "wrong_transaction":
            events[-1]["transaction_id"] = "store-trial"
        else:
            events[-1]["environment"] = "PRODUCTION"
    with pytest.raises(EvidenceError):
        build_evidence(snapshot, transactions, events, PRODUCTS, now=observed)


def trial_grace_fixture():
    snapshot, transactions, events = prepaid_fixture()
    observed = BOUNDARY + timedelta(hours=1)
    grace_end = BOUNDARY + timedelta(days=3)
    snapshot.update({"status": "in_grace_period", "pending_changes": None,
                     "pending_payment": True, "auto_renewal_status": "will_not_renew",
                     "ends_at": ms(BOUNDARY)})
    transactions = transactions[:1]
    transactions[0]["effective_expiration_date"] = ms(grace_end)
    return snapshot, transactions, events[:1], observed, grace_end


def test_free_trial_grace_refresh_preserves_billing_kind_with_and_without_original_event():
    snapshot, transactions, events, observed, grace_end = trial_grace_fixture()
    original = build_evidence(snapshot, transactions, events, PRODUCTS, now=observed)
    refreshed = build_evidence(snapshot, transactions, events, PRODUCTS,
                               now=observed + timedelta(minutes=1), previous=original)
    without_original_event = build_evidence(snapshot, transactions, [], PRODUCTS,
                                            now=observed + timedelta(minutes=2), previous=refreshed)
    for proof in (original, refreshed, without_original_event):
        period = next(p for p in proof["periods"] if p["transaction_id"] == "store-trial")
        assert period["kind"] == "grace" and period["billing_kind"] == "trial"
        active = resolve_access(row(proof), observed + timedelta(minutes=2))
        assert active is not None and active.status == "grace_period"
        assert active.expires_at == grace_end


def test_free_trial_grace_can_transition_to_a_new_verified_paid_transaction():
    snapshot, transactions, events, observed, _ = trial_grace_fixture()
    previous = build_evidence(snapshot, transactions, events, PRODUCTS, now=observed)
    paid_start = BOUNDARY + timedelta(days=1)
    paid_end = paid_start + timedelta(days=365)
    snapshot.update({"status": "active", "pending_payment": False,
                     "auto_renewal_status": "will_renew", "store_subscription_identifier": "new-paid",
                     "current_period_starts_at": ms(paid_start), "current_period_ends_at": ms(paid_end),
                     "ends_at": ms(paid_end)})
    transactions.append({"id": "new-paid", "purchased_at": ms(paid_start),
                         "effective_expiration_date": ms(paid_end), "product_store_identifier": YEARLY,
                         "revenue_in_local_currency": {"gross": 5500}})
    paid_event = {"type": "RENEWAL", "transaction_id": "new-paid", "environment": "SANDBOX",
                  "app_user_id": OWNER_A, "period_type": "NORMAL", "price_in_purchased_currency": 5500,
                  "purchased_at_ms": ms(paid_start), "expiration_at_ms": ms(paid_end)}
    proof = build_evidence(snapshot, transactions, [paid_event], PRODUCTS,
                           now=paid_start + timedelta(minutes=1), previous=previous)
    active = resolve_access(row(proof), paid_start + timedelta(minutes=1))
    assert active is not None and active.status == "active" and active.plan == "yearly"
    assert active.store_trial_ends_at is None and active.expires_at == paid_end
    kinds = {p["transaction_id"]: p["billing_kind"] for p in proof["periods"]}
    assert kinds == {"store-trial": "trial", "new-paid": "paid"}
