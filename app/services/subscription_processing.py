"""Staged subscription processing for explicitly adopted subscriptions.

Provider reads happen after releasing the inbox claim. Applying a prepared read
rechecks every baseline under ownership/user/row locks. Old financial stages are
never rerun just because access or a purchase bonus still needs verification.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import InvalidOperation
from types import SimpleNamespace
import uuid

from sqlalchemy import or_, select

from app.core.advisory_lock import advisory_xact_lock
from app.models.payment import Payment
from app.models.hay_transaction import HayTransaction
from app.models.profile import Profile
from app.models.revenuecat_event import RevenuecatEvent
from app.models.subscription import Subscription
from app.models.subscription_hay_grant import SubscriptionHayGrant
from app.services import revenuecat, subscription_access as access
from app.services.config_store import get_config_values

ENABLED = "subscription_access_evidence_enabled"
COHORT = "subscription_access_evidence_users"
_FIELDS = (
    "id", "user_id", "rc_subscription_id", "ownership_changed_at", "plan", "status",
    "original_transaction_id", "latest_transaction_id", "purchased_at", "expires_at",
    "store_trial_ends_at", "last_event_at", "auto_renew_enabled", "environment", "access_evidence",
)
_PAY_FIELDS = (
    "id", "user_id", "subscription_id", "store_transaction_id", "status", "store",
    "subscription_plan", "subscription_bonus_review", "subscription_hay_grant_id",
)
_TYPES = {"INITIAL_PURCHASE", "RENEWAL", "UNCANCELLATION", "PRODUCT_CHANGE",
          "SUBSCRIPTION_EXTENDED", "CANCELLATION", "EXPIRATION", "BILLING_ISSUE",
          "REFUND_REVERSED", "TRANSFER"}
_TERMINAL = {"done", "skipped", "not_applicable"}
_FINANCE = ("payment_record", "bonus", "refund_or_reversal")


def baseline(sub) -> dict:
    return {key: deepcopy(getattr(sub, key, None)) for key in _FIELDS}


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


@dataclass
class Target:
    before: dict | None
    snapshot: dict
    transactions: list[dict]
    evidence: dict


@dataclass
class Prepared:
    before: list[dict]
    pay_before: dict | None
    started_at: datetime
    targets: list[Target] = field(default_factory=list)
    error: str | None = None
    bonus_review: str = "held:provider_history_unavailable"


def new_state(event: dict) -> dict:
    purchase = event.get("type") in {"INITIAL_PURCHASE", "RENEWAL"}
    refund = financial_action(event) is not None
    return {"version": 1, "payment_record": "pending" if purchase else "not_applicable",
            "bonus": "pending" if purchase else "not_applicable",
            "refund_or_reversal": "pending" if refund else "not_applicable",
            "access_state": "pending", "tries": {}}


_RC_ANONYMOUS_PREFIX = "$RCAnonymousID:"


def _anonymous_alias_of_owner(event: dict, original_customer_id, owner_id: str) -> bool:
    """첫 구매자가 지금 고객의 로그인 전 익명 ID인가. RevenueCat이 둘을 같은 고객(aliases)으로 묶었을 때만 참."""
    aliases = event.get("aliases")
    return (
        isinstance(original_customer_id, str)
        and original_customer_id.startswith(_RC_ANONYMOUS_PREFIX)
        and isinstance(aliases, list)
        and original_customer_id in aliases
        and owner_id in aliases
        and event.get("app_user_id") == owner_id
    )


async def _owned_same_chain_before(session, sub, event: dict, paid_at) -> bool:
    """결제 전부터 이 사용자가 같은 스토어 원거래의 구독을 갖고 있었다는 서버 기록이 있는가."""
    original = event.get("original_transaction_id")
    if not isinstance(original, str) or not original or paid_at is None:
        return False
    found = (await session.execute(
        select(Subscription.id).where(
            Subscription.user_id == sub.user_id,
            Subscription.id != sub.id,
            Subscription.original_transaction_id == original,
            Subscription.created_at <= paid_at,
            or_(Subscription.ownership_changed_at.is_(None), Subscription.ownership_changed_at <= paid_at),
        ).limit(1)
    )).scalar_one_or_none()
    return found is not None


def financial_action(event: dict) -> str | None:
    if event.get("type") == "REFUND_REVERSED":
        return "reverse"
    if event.get("type") == "CANCELLATION" and event.get("cancel_reason") == "CUSTOMER_SUPPORT":
        return "refund"
    return None


def validate_state(state: dict) -> dict:
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
        raise access.EvidenceError("unsupported processing state")
    if any(state.get(key) not in _TERMINAL | {"pending", "manual"} for key in _FINANCE):
        raise access.EvidenceError("invalid financial stage")
    if state.get("access_state") not in {"pending", "verified", "manual"}:
        raise access.EvidenceError("invalid access stage")
    tries = state.get("tries")
    if not isinstance(tries, dict) or any(
        key not in (*_FINANCE, "access_state") or type(value) is not int or not 0 <= value <= 100
        for key, value in tries.items()
    ):
        raise access.EvidenceError("invalid processing attempt counts")
    return state


def finish_attempt(state: dict) -> tuple[str, bool]:
    """Bound each incomplete stage, preserving already verified/done stages."""
    validate_state(state)
    tries = dict(state["tries"])
    for key in (*_FINANCE, "access_state"):
        if state[key] == "pending":
            tries[key] = int(tries.get(key, 0)) + 1
            if tries[key] >= 5:
                state[key] = "manual"
    state["tries"] = tries
    state["finance_done"] = all(state[key] in _TERMINAL for key in _FINANCE)
    pending = any(state[key] == "pending" for key in (*_FINANCE, "access_state"))
    manual = any(state[key] == "manual" for key in (*_FINANCE, "access_state"))
    return ("pending" if pending else "failed" if manual else "processed"), pending


async def selected(session, event: dict, state: dict | None) -> Prepared | None:
    """Only supported subscription events can enter the new path; packs stay legacy."""
    from app.services import subscription as old

    if state is not None:
        validate_state(state)
    if event.get("type") not in _TYPES:
        if state is not None:
            raise access.EvidenceError("staged event type unavailable")
        return None
    tx = old._txn_id(event.get("transaction_id"), field="transaction_id")
    original = old._txn_id(event.get("original_transaction_id"), field="original_transaction_id")
    users = {_uuid(event.get("app_user_id"))}
    if event.get("type") == "TRANSFER":
        users.update(_uuid(x) for x in [*event.get("transferred_from", []), *event.get("transferred_to", [])])
    users.discard(None)
    if not tx and not original and event.get("type") != "TRANSFER":
        return None
    if event.get("type") == "TRANSFER" and not users:
        return None
    pay = (await session.execute(select(Payment).where(Payment.store_transaction_id == tx))).scalars().first() if tx else None
    if pay is not None and pay.subscription_id is None:
        return None
    conditions = []
    if tx or original:
        ids = [x for x in (tx, original) if x]
        conditions.extend([Subscription.original_transaction_id.in_(ids), Subscription.latest_transaction_id.in_(ids)])
        if tx:
            conditions.append(Subscription.access_evidence["periods"].contains([{"transaction_id": tx}]))
    if pay is not None:
        conditions.append(Subscription.id == pay.subscription_id)
    if event.get("type") == "TRANSFER":
        conditions.append(Subscription.user_id.in_(users))
        for uid in users:
            conditions.append(Subscription.access_evidence["periods"].contains([{"owner_id": str(uid)}]))
            conditions.append(Subscription.access_evidence["legacy_history"].contains([{"owner_id": str(uid)}]))
    # A new renewal can have an Apple chain root different from this RC object's
    # first transaction. Adoption must remain sticky after product/object changes.
    conditions.append(Subscription.user_id.in_(users) & Subscription.access_evidence.is_not(None))
    if original:
        conditions.append(Subscription.access_evidence["store_original_transaction_ids"].contains([original]))
    subs = list((await session.execute(select(Subscription).where(or_(*conditions)))).scalars())
    adopted = any(s.access_evidence is not None for s in subs)
    if not adopted and state is None:
        payload = RevenuecatEvent.payload
        prior_stage = (await session.execute(select(RevenuecatEvent.event_id).where(
            RevenuecatEvent.processing_state.is_not(None),
            or_(payload["app_user_id"].astext.in_([str(u) for u in users]),
                payload["original_transaction_id"].astext.in_([x for x in (original, tx) if x]),
                payload["transaction_id"].astext == tx if tx else False),
        ).limit(1))).scalars().first()
        adopted = prior_stage is not None
    if not adopted and state is None:
        config = await get_config_values(session, [ENABLED, COHORT])
        scope = config.get(COHORT, [])
        allowed = isinstance(scope, list) and ("*" in scope or bool({str(u) for u in users} & set(
            x for x in scope if isinstance(x, str))))
        if config.get(ENABLED) is not True or not allowed:
            return None
    return Prepared([baseline(s) for s in subs],
                    {k: deepcopy(getattr(pay, k)) for k in _PAY_FIELDS} if pay else None,
                    datetime.now(timezone.utc))


def _legacy_history(before: dict, evidence: dict, now: datetime) -> list[dict]:
    start, end = before.get("purchased_at"), before.get("expires_at")
    if (before.get("status") not in {"active", "grace_period", "expired"}
            or start is None or end is None or before.get("user_id") is None):
        return []
    # Never retain the corrupt annual horizon as new future proof.
    end = min(end, now)
    proven_ends = [access.instant(p["end"]) for p in evidence["periods"]
                   if p["owner_id"] == str(before["user_id"])]
    if proven_ends:
        end = min(end, max(proven_ends))
    if end <= start:
        return []
    return [{"transaction_id": before.get("latest_transaction_id") or before["original_transaction_id"],
             "rc_subscription_id": evidence["rc_subscription_id"], "store": evidence["store"],
             "environment": evidence["environment"], "owner_id": str(before["user_id"]),
             "owner_start": before["ownership_changed_at"].isoformat() if before.get("ownership_changed_at") else None,
             "owner_end": None, "start": start.isoformat(), "end": end.isoformat(),
             "kind": "paid", "plan": before["plan"], "source": "legacy_before_adoption"}]


async def fetch(session, event: dict, prepared: Prepared) -> Prepared:
    """No inbox/global/user row locks may be held by the caller during these reads."""
    from app.services import subscription as old

    try:
        async with asyncio.timeout(25):
            pairs = []
            if event.get("type") == "TRANSFER":
                for before in prepared.before:
                    if not before.get("rc_subscription_id"):
                        raise access.EvidenceError("transfer object unavailable")
                    snap = await revenuecat.get_subscription(before["rc_subscription_id"])
                    pairs.append((snap, await revenuecat.subscription_transactions(snap["id"])))
            else:
                pairs.append(await old._snapshot_for_transaction(
                    str(event.get("transaction_id") or event.get("original_transaction_id")), event.get("environment")))
            products = old._STORE_PRODUCTS | await old._google_products(session)
            for snapshot, transactions in pairs:
                tx_ids = [str(tx["id"]) for tx in transactions]
                matches = list((await session.execute(select(Subscription).where(or_(
                    Subscription.rc_subscription_id == snapshot["id"],
                    Subscription.original_transaction_id.in_(tx_ids),
                    Subscription.latest_transaction_id.in_(tx_ids),
                )))).scalars())
                if len(matches) > 1:
                    raise access.EvidenceError("multiple local subscriptions match object")
                before = baseline(matches[0]) if matches else None
                if before is not None and before not in prepared.before:
                    prepared.before.append(before)
                if before and before.get("rc_subscription_id") not in {None, snapshot["id"]}:
                    raise access.EvidenceError("different local RC object")
                if before and before.get("environment") not in {None, str(snapshot.get("environment", "")).upper()}:
                    raise access.EvidenceError("local environment mismatch")
                proofs = list((await session.execute(select(RevenuecatEvent.payload).where(
                    RevenuecatEvent.payload["transaction_id"].astext.in_(tx_ids),
                    RevenuecatEvent.payload["environment"].astext == str(snapshot.get("environment", "")).upper(),
                ).limit(2001))).scalars())
                if len(proofs) > 2000:
                    raise access.EvidenceError("transaction proof history requires review")
                proofs.append(event)
                now = datetime.now(timezone.utc)
                owner_changed = before is not None and str(before["user_id"]) != snapshot.get("customer_id")
                evidence = access.build_evidence(
                    snapshot, transactions, proofs, products, now=now,
                    previous=before["access_evidence"] if before else None,
                    ownership_since=(now if owner_changed else before.get("ownership_changed_at")) if before else None,
                )
                if before and before["access_evidence"] is None:
                    evidence["legacy_history"] = _legacy_history(before, evidence, now)
                prepared.targets.append(Target(before, snapshot, transactions, evidence))
            if event.get("type") in {"INITIAL_PURCHASE", "RENEWAL"} and prepared.targets:
                target = next((t for t in prepared.targets if event.get("transaction_id") in {
                    tx["id"] for tx in t.transactions}), None)
                if target is not None:
                    candidate = SimpleNamespace(user_id=_uuid(target.snapshot["customer_id"]),
                                                environment=target.evidence["environment"])
                    plan, _ = await old._resolve_plan(session, event.get("type"), event)
                    if candidate.user_id is not None and plan:
                        prepared.bonus_review = await old._bonus_review(session, candidate, event, plan)
    except (revenuecat.RevenueCatError, access.EvidenceError, TimeoutError) as exc:
        prepared.targets = []
        prepared.error = str(exc)[:200] or "provider verification timeout"
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, InvalidOperation):
        prepared.targets = []
        prepared.error = "provider verification payload invalid"
    return prepared


async def _financial_order(session, event: dict, pay: Payment, action: str) -> str:
    """All committed stages count, including pending/failed access retries."""
    timestamp = access.instant(event.get("event_timestamp_ms"))
    if timestamp is None:
        return "manual"
    rows = list((await session.execute(select(RevenuecatEvent).where(
        RevenuecatEvent.payload["transaction_id"].astext == pay.store_transaction_id,
    ).limit(2001))).scalars())
    if len(rows) > 2000:
        return "manual"
    applied = []
    for row in rows:
        if row.event_id == event.get("id"):
            continue
        payload, state = row.payload, row.processing_state
        kind = financial_action(payload)
        if kind is None:
            continue
        stamp = access.instant(payload.get("event_timestamp_ms"))
        committed = isinstance(state, dict) and state.get("refund_or_reversal") == "done"
        legacy = state is None and row.status == "processed"
        if stamp is not None and (committed or legacy):
            applied.append((stamp, kind))
    if not applied:
        # A refunded legacy Payment without a retained ordering proof is not
        # permission to reconstruct money. Already completed same-state refunds
        # can be skipped, but a reversal waits for the original refund proof.
        if ":refund_reversed" in (pay.subscription_bonus_review or ""):
            return "manual"
        return "confirm" if action == "refund" and pay.status == "refunded" else (
            "pending" if action == "reverse" else "apply")
    newest = max(stamp for stamp, _ in applied)
    kinds = {kind for stamp, kind in applied if stamp == newest}
    if len(kinds) != 1:
        return "manual"
    last_kind = next(iter(kinds))
    expected = "refunded" if last_kind == "refund" else "paid"
    if pay.status != expected:
        return "manual"
    if timestamp < newest:
        return "skip"
    if timestamp == newest:
        return "confirm" if action == last_kind else "manual"
    return "confirm" if action == last_kind else "apply"


async def _unresolved_adjustment(session, tx_id: str) -> str | None:
    rows = list((await session.execute(select(RevenuecatEvent).where(
        RevenuecatEvent.payload["transaction_id"].astext == tx_id,
    ).limit(2001))).scalars())
    if len(rows) > 2000:
        return "manual"
    completed, unresolved = [], []
    for row in rows:
        if financial_action(row.payload) is None:
            continue
        stamp = access.instant(row.payload.get("event_timestamp_ms"))
        state = row.processing_state
        done = isinstance(state, dict) and state.get("refund_or_reversal") == "done"
        if done or (state is None and row.status == "processed"):
            if stamp is not None:
                completed.append(stamp)
        elif row.status in {"pending", "failed"}:
            unresolved.append((stamp, row.status))
    newest = max(completed) if completed else None
    relevant = [status for stamp, status in unresolved
                if stamp is None or newest is None or stamp >= newest]
    return ("manual" if "failed" in relevant else "pending") if relevant else None


async def _grant_proof(session, pay, grant) -> str | None:
    source = await session.get(HayTransaction, grant.hay_transaction_id) if grant.hay_transaction_id else None
    if (source is None or source.user_id != pay.user_id or source.type != "subscription_grant"
            or source.amount <= 0):
        return "bonus source ledger unavailable"
    if grant.revoked_at is not None:
        clawback = await session.get(HayTransaction, grant.clawback_hay_transaction_id) if grant.clawback_hay_transaction_id else None
        if (clawback is None or clawback.user_id != pay.user_id or clawback.type != "refund_revoke"
                or clawback.amount != -source.amount):
            return "bonus clawback ledger unavailable"
    return None


async def _finance(session, event: dict, state: dict, pay: Payment | None, sub, prepared: Prepared):
    from app.services import subscription as old

    action = financial_action(event)
    if action is not None and state["refund_or_reversal"] == "pending":
        if pay is None:
            tx = event.get("transaction_id")
            evidence_sets = [t.evidence for t in prepared.targets]
            evidence_sets.extend(b["access_evidence"] for b in prepared.before if b.get("access_evidence") is not None)
            free_proven = any(p.get("transaction_id") == tx and p.get("kind") == "trial"
                              for evidence in evidence_sets for p in evidence.get("periods", []))
            if free_proven:
                state["refund_or_reversal"] = "not_applicable"
            else:
                state["reason"] = "refund payment unavailable"
            return
        decision = await _financial_order(session, event, pay, action)
        if decision not in {"apply", "confirm"}:
            state["refund_or_reversal"] = {"skip": "skipped", "manual": "manual"}.get(decision, "pending")
            state["reason"] = "financial ordering " + decision
            return
        state["payment_id"] = str(pay.id)
        state["finance_event_at"] = access.instant(event["event_timestamp_ms"]).isoformat()
        state["finance_action"] = action
        if decision == "confirm":
            # Same financial state, but a NEWER event is still an ordering proof.
            # Preserve it as done so a late opposite event cannot undo it.
            state["refund_or_reversal"] = "done"
            if pay.subscription_bonus_review is None:
                state["bonus"] = "manual"
                state["reason"] = "legacy bonus link requires review"
            elif pay.subscription_hay_grant_id:
                grant = await session.get(SubscriptionHayGrant, pay.subscription_hay_grant_id, with_for_update=True)
                if (grant is None or grant.user_id != pay.user_id or grant.plan != pay.subscription_plan
                        or (action == "refund") != (grant.revoked_at is not None)):
                    state["bonus"] = "manual"
                    state["reason"] = "confirmed payment has unresolved bonus history"
                else:
                    problem = await _grant_proof(session, pay, grant)
                    if problem:
                        state["bonus"] = "manual"
                        state["reason"] = problem
            return
        if pay.subscription_bonus_review is None:
            # Keep payment refund correct without guessing a missing grant link.
            pay.status = "refunded" if action == "refund" else "paid"
            state["bonus"] = "manual"
            state["reason"] = "legacy bonus link requires review"
        else:
            grant = await session.get(SubscriptionHayGrant, pay.subscription_hay_grant_id, with_for_update=True) if pay.subscription_hay_grant_id else None
            if pay.subscription_hay_grant_id and (grant is None or grant.user_id != pay.user_id
                                                   or grant.plan != pay.subscription_plan):
                pay.status = "refunded" if action == "refund" else "paid"
                state["bonus"] = "manual"
                state["reason"] = "bonus grant unavailable"
            elif grant is not None and (problem := await _grant_proof(session, pay, grant)):
                pay.status = "refunded" if action == "refund" else "paid"
                state["bonus"] = "manual"
                state["reason"] = problem
            elif grant is not None and action == "reverse" and grant.revoked_at is None:
                pay.status = "paid"
                state["bonus"] = "manual"
                state["reason"] = "bonus clawback unverified; no duplicate restore"
            elif action == "refund":
                await old._refund_subscription(session, pay, access.instant(event.get("event_timestamp_ms")), update_access=False)
            elif pay.status == "refunded":
                await old._reverse_subscription_refund(session, pay, access.instant(event.get("event_timestamp_ms")), update_access=False)
        state["refund_or_reversal"] = "done"
    if event.get("type") not in {"INITIAL_PURCHASE", "RENEWAL"}:
        return
    if pay is not None and pay.status != "paid":
        state["payment_record"] = "done"
        state["payment_id"] = str(pay.id)
        state["bonus"] = "skipped"
        state["reason"] = "refunded payment: no bonus grant"
        return
    if old._free_subscription_period(event):
        # Same as the legacy path: a store free period consumes the app's Android
        # trial-offer claim. The UPDATE only touches unredeemed rows, so retries are no-ops.
        uid = _uuid(event.get("app_user_id"))
        if uid is not None:
            await old._mark_store_trial_offer_redeemed(session, uid, event)
        state["payment_record"] = state["bonus"] = "not_applicable"
        return
    if sub is None or not prepared.targets:
        return  # New payment/bonus needs provider ownership and transaction proof.
    target = next((t for t in prepared.targets if t.snapshot["id"] == sub.rc_subscription_id), None)
    if target is None:
        return
    if str(sub.user_id) != target.snapshot.get("customer_id"):
        state["reason"] = "purchase current ownership requires review"
        return
    uid = _uuid(event.get("app_user_id"))
    start = access.instant(event.get("purchased_at_ms"))
    stamp = access.instant(event.get("event_timestamp_ms"))
    first = min(target.transactions, key=lambda tx: tx.get("purchased_at") or 0)
    original_first_purchase = (
        target.before is None and event.get("type") == "INITIAL_PURCHASE"
        and event.get("transaction_id") == first["id"]
        and target.snapshot.get("original_customer_id") == str(sub.user_id)
    )
    # 새로 만든 구독 행의 소유 시작은 처리 시각이라 결제 시각보다 늦다(예: 체험 만료 뒤 다시 결제).
    # 아래를 모두 만족할 때만 본인 결제로 본다. aliases만으로는 판단하지 않는다(기기 공유·구매 복원).
    #  1) 가족 공유 결제가 아니다  2) 첫 구매자가 로그인 전 익명 ID이고 RevenueCat이 지금 고객과 같은
    #  고객으로 묶었다  3) 결제 전부터 이 사용자가 같은 원거래의 구독을 갖고 있었다는 서버 기록이 있다.
    own_anonymous_original = (
        target.before is None
        and event.get("is_family_share") is False
        and _anonymous_alias_of_owner(event, target.snapshot.get("original_customer_id"), str(sub.user_id))
        and await _owned_same_chain_before(session, sub, event, start)
    )
    unknown_new_owner = (target.before is None and not own_anonymous_original
                         and target.snapshot.get("original_customer_id") != str(sub.user_id))
    predates_ownership = not own_anonymous_original and sub.ownership_changed_at and (
        start is None or stamp is None or start < sub.ownership_changed_at or stamp < sub.ownership_changed_at)
    if pay is None and uid != sub.user_id:
        state["payment_record"] = state["bonus"] = "skipped"
        state["reason"] = "past owner purchase: financial ownership preserved"
        return
    if pay is None and (unknown_new_owner or (predates_ownership and not original_first_purchase)):
        state["payment_record"] = state["bonus"] = "manual"
        state["reason"] = "historical payer ownership unverified: do not reconstruct money"
        return
    if state["payment_record"] == "pending":
        if pay is None:
            amount, _ = old._event_money(event)
            if amount is None or amount <= 0:
                state["payment_record"] = "manual"
                state["bonus"] = "manual"
                state["reason"] = "purchase amount unavailable"
                return
            pay = await old._record_subscription_payment(session, sub, event)
        if pay is None:
            return
        old._validate_subscription_payment(pay, sub)
        state["payment_record"] = "done"
        state["payment_id"] = str(pay.id)
    if state["bonus"] != "pending" or pay is None:
        return
    if pay.status != "paid":
        state["bonus"] = "skipped"
        state["reason"] = "refunded payment: no bonus grant"
        return
    if pay.user_id != uid or pay.user_id != sub.user_id:
        state["bonus"] = "skipped"
        return
    unresolved = await _unresolved_adjustment(session, pay.store_transaction_id)
    if unresolved is not None:
        state["bonus"] = unresolved
        state["reason"] = "unresolved earlier refund/reversal: bonus withheld"
        return
    if not (pay.subscription_bonus_review or "").startswith("held:"):
        state["bonus"] = "done" if pay.subscription_bonus_review is not None else "manual"
        return
    review = prepared.bonus_review
    if review.startswith("held:"):
        state["reason"] = review
        return
    if review == "eligible":
        grant = await old._grant_if_first(session, pay.user_id, pay.subscription_plan)
        pay.subscription_hay_grant_id = grant.id if grant else None
        review = "granted" if grant else "not_granted:user_plan_previously_granted"
    pay.subscription_bonus_review = review
    state["bonus"] = "done"


async def apply(session, row: RevenuecatEvent, prepared: Prepared):
    from app.services import subscription as old

    event = dict(row.payload)
    state = deepcopy(row.processing_state) if row.processing_state is not None else new_state(event)
    validate_state(state)
    await old._ownership_lock(session)
    users = {_uuid(event.get("app_user_id"))}
    users.update(b["user_id"] for b in prepared.before)
    users.update(_uuid(t.snapshot["customer_id"]) for t in prepared.targets)
    if prepared.pay_before:
        users.add(prepared.pay_before["user_id"])
    users.discard(None)
    for uid in sorted(users, key=str):
        await advisory_xact_lock(session, uid)
    locked = {}
    for before in sorted(prepared.before, key=lambda b: str(b["id"])):
        sub = await session.get(Subscription, before["id"], with_for_update=True, populate_existing=True)
        if sub is None or baseline(sub) != before:
            state["reason"] = "subscription changed during provider read"
            row.processing_state = state
            return old.HandlerResult(old.HANDLED_PENDING, state["reason"])
        locked[sub.id] = sub
    pay = None
    if prepared.pay_before:
        pay = await session.get(Payment, prepared.pay_before["id"], with_for_update=True, populate_existing=True)
        if pay is None or any(getattr(pay, key) != val for key, val in prepared.pay_before.items()):
            state["reason"] = "payment changed during provider read"
            row.processing_state = state
            return old.HandlerResult(old.HANDLED_PENDING, state["reason"])
    fresh = (datetime.now(timezone.utc) - prepared.started_at).total_seconds() < 60
    if not fresh:
        prepared.targets = []
        prepared.error = "provider read expired"
    event_sub = locked.get(pay.subscription_id) if pay else None
    for target in prepared.targets if state["access_state"] == "pending" else []:
        owner = _uuid(target.snapshot["customer_id"])
        if owner is not None and await session.get(Profile, owner) is None:
            owner = None
        sub = locked.get(target.before["id"]) if target.before else None
        if sub is None:
            if owner is None:
                continue  # No app owner: do not create an unowned subscription.
            # A concurrent first receipt may have committed during the provider read.
            existing = (await session.execute(select(Subscription.id).where(
                Subscription.rc_subscription_id == target.snapshot["id"]))).scalar_one_or_none()
            if existing is not None:
                state["reason"] = "new subscription appeared during provider read"
                row.processing_state = state
                return old.HandlerResult(old.HANDLED_PENDING, state["reason"])
            first = min(target.transactions, key=lambda tx: tx.get("purchased_at") or 0)
            current = next(tx for tx in target.transactions if tx["id"] == target.evidence["current_transaction_id"])
            plan, _ = await old._resolve_plan(session, None, {"product_id": current.get("product_store_identifier")})
            if plan is None:
                raise access.EvidenceError("new subscription product unavailable")
            sub = Subscription(id=uuid.uuid4(), user_id=owner, plan=plan,
                               original_transaction_id=str(first["id"]), status="expired",
                               ownership_changed_at=access.instant(target.evidence["verified_at"]))
            session.add(sub)
        if sub.user_id != owner:
            sub.user_id = owner
            sub.ownership_changed_at = access.instant(target.evidence["verified_at"])
        access.project(sub, target.evidence, datetime.now(timezone.utc))
        old._bump_last_event(sub, access.instant(event.get("event_timestamp_ms")))
        if event.get("transaction_id") in {tx["id"] for tx in target.transactions}:
            event_sub = sub
    if state["access_state"] != "manual" and prepared.targets:
        state["access_state"] = "verified"
    if prepared.error:
        state["reason"] = prepared.error
    if pay is not None:
        sub = locked.get(pay.subscription_id)
        if sub is None or sub.environment != str(event.get("environment", "")).upper() or pay.store != old._store_of(event):
            state["refund_or_reversal"] = "manual"
            state["reason"] = "payment store or environment mismatch"
            row.processing_state = state
            return old.HandlerResult(old.HANDLED_PENDING, state["reason"])
    await _finance(session, event, state, pay, event_sub, prepared)
    state["verified_revision"] = {str(t.before["id"]): t.evidence["revision"]
                                  for t in prepared.targets if t.before}
    row.processing_state = deepcopy(state)
    return old.HandlerResult(old.HANDLED_PENDING, state.get("reason"))
