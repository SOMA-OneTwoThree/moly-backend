"""Verified store periods. Current access and historical access use the same time bounds.

The JSON is replaced as a whole so SQLAlchemy persists every revision. No network or
financial writes belong here. A NULL evidence value retains the released legacy policy.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

VERSION = 1


class EvidenceError(ValueError):
    """Missing or conflicting proof; preserve the previous verified revision."""


def instant(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = (datetime.fromisoformat(value.replace("Z", "+00:00"))
                  if isinstance(value, str) else datetime.fromtimestamp(value / 1000, timezone.utc))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _required(value: Any, name: str) -> datetime:
    result = instant(value)
    if result is None:
        raise EvidenceError(f"invalid {name}")
    return result


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise EvidenceError(f"invalid {name}")
    return value


def validate(evidence: dict) -> dict:
    if not isinstance(evidence, dict) or type(evidence.get("version")) is not int or evidence["version"] != VERSION:
        raise EvidenceError("unsupported access evidence version")
    revision = evidence.get("revision")
    if type(revision) is not int or revision < 1:
        raise EvidenceError("invalid access evidence revision")
    _required(evidence.get("verified_at"), "verified_at")
    if evidence.get("environment") not in {"SANDBOX", "PRODUCTION"}:
        raise EvidenceError("invalid evidence environment")
    if evidence.get("store") not in {"app_store", "play_store"}:
        raise EvidenceError("invalid evidence store")
    _identifier(evidence.get("rc_subscription_id"), "subscription object")
    _identifier(evidence.get("owner_id"), "current owner")
    _identifier(evidence.get("current_transaction_id"), "current transaction")
    if type(evidence.get("auto_renew_enabled")) is not bool:
        raise EvidenceError("invalid auto renewal evidence")
    periods = evidence.get("periods")
    if not isinstance(periods, list) or len(periods) > 2000:
        raise EvidenceError("invalid access periods")
    legacy = evidence.get("legacy_history", [])
    if not isinstance(legacy, list) or len(legacy) > 2000:
        raise EvidenceError("invalid legacy history")
    for period in [*periods, *legacy]:
        if not isinstance(period, dict):
            raise EvidenceError("invalid access period")
        for name in ("transaction_id", "rc_subscription_id", "owner_id"):
            _identifier(period.get(name), name)
        start = _required(period.get("start"), "period start")
        end = _required(period.get("end"), "period end")
        if end < start or period.get("plan") not in {"monthly", "yearly"}:
            raise EvidenceError("invalid access period bounds or product")
        if period.get("kind") not in {"trial", "paid", "grace"}:
            raise EvidenceError("invalid access period kind")
        if period.get("billing_kind") not in {None, "trial", "paid"}:
            raise EvidenceError("invalid billing period kind")
        if period.get("store") != evidence["store"] or period.get("environment") != evidence["environment"]:
            raise EvidenceError("access period store mismatch")
        for key in ("owner_start", "owner_end"):
            if period.get(key) is not None:
                _required(period[key], key)
    return evidence


@dataclass(frozen=True)
class AccessView:
    subscription: Any
    plan: str
    status: str
    purchased_at: datetime
    expires_at: datetime
    store_trial_ends_at: datetime | None

    def __getattr__(self, key):
        return getattr(self.subscription, key)


def resolve_access(sub: Any, at: datetime, owner_id: Any = None) -> AccessView | Any | None:
    """Half-open periods: a prepaid period takes over without requiring another webhook."""
    evidence = getattr(sub, "access_evidence", None)
    if evidence is None:
        owner_matches = owner_id is None or str(getattr(sub, "user_id", "")) == str(owner_id)
        expires = sub.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return sub if (owner_matches and sub.status in {"active", "grace_period"}
                       and expires is not None and expires > at) else None
    validate(evidence)
    owner = str(owner_id if owner_id is not None else getattr(sub, "user_id", ""))
    matches = []
    for period in evidence["periods"]:
        if period["owner_id"] != owner:
            continue
        start, end = _required(period["start"], "start"), _required(period["end"], "end")
        lower, upper = instant(period.get("owner_start")), instant(period.get("owner_end"))
        if start <= at < end and (lower is None or lower <= at) and (upper is None or at < upper):
            matches.append(period)
    # Preserve the already-released historical policy only BEFORE first adoption.
    # This cannot extend current/future rights or infer ownership from RC aliases.
    for period in evidence.get("legacy_history", []):
        if period.get("owner_id") != owner:
            continue
        start, end = instant(period.get("start")), instant(period.get("end"))
        lower = instant(period.get("owner_start"))
        if start is not None and end is not None and start <= at < end and (lower is None or lower <= at):
            matches.append(period)
    if not matches:
        return None
    # Product changes can overlap. The later verified period determines the product.
    period = max(matches, key=lambda p: (_required(p["start"], "start"), p["transaction_id"]))
    start, end = _required(period["start"], "start"), _required(period["end"], "end")
    return AccessView(sub, period["plan"], "grace_period" if period["kind"] == "grace" else "active",
                      start, end, end if period["kind"] == "trial" else None)


def _positive(value: Any) -> bool:
    try:
        number = Decimal(str(value))
        return number.is_finite() and number > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def _transaction_plan(product: Any, products: dict[str, str], store: str) -> str | None:
    """Google Play는 '구독ID:basePlanId'로 보낸다. 웹훅 요금제 판정과 같게 ':' 앞으로 다시 찾는다."""
    if not isinstance(product, str):
        return None
    plan = products.get(product)
    if plan is None and store == "play_store" and ":" in product:
        plan = products.get(product.split(":", 1)[0])
    return plan


def build_evidence(
    snapshot: dict, transactions: list[dict], events: list[dict], products: dict[str, str], *,
    now: datetime, previous: dict | None = None, ownership_since: datetime | None = None,
) -> dict:
    """Normalize a complete v2 subscription object, never max(next end, trial end).

    SubscriptionTransaction.id is the STORE transaction ID in this endpoint. An
    already-paid next period needs a matching transaction and paid proof; will_renew
    or ends_at alone is insufficient. History outside this RC object stays separate.
    """
    rc_id = _identifier(snapshot.get("id"), "RC object")
    owner = _identifier(snapshot.get("customer_id"), "owner")
    environment = str(snapshot.get("environment", "")).upper()
    store = snapshot.get("store")
    gives_access = snapshot.get("gives_access")
    if environment not in {"PRODUCTION", "SANDBOX"} or store not in {"app_store", "play_store"}:
        raise EvidenceError("unsupported store or environment")
    if type(gives_access) is not bool:
        raise EvidenceError("current access unverified")
    latest = _identifier(snapshot.get("store_subscription_identifier"), "current transaction")
    txs = {_identifier(tx.get("id"), "transaction"): tx for tx in transactions}
    if latest not in txs or len(txs) != len(transactions):
        raise EvidenceError("incomplete or duplicate transaction chain")
    if previous is not None:
        validate(previous)
        if previous["store"] != store or previous["environment"] != environment:
            raise EvidenceError("previous evidence store mismatch")
        if previous["rc_subscription_id"] != rc_id:
            raise EvidenceError("different RC object requires a separate subscription row")
    history = deepcopy(previous["periods"]) if previous else []
    previous_owner = previous.get("owner_id") if previous else None
    changed_owner = previous is not None and previous_owner != owner
    if previous is None and ownership_since is None:
        # Current/original customer equality cannot disprove an unobserved round trip.
        # Historical legacy eligibility is preserved separately by the adopter.
        ownership_since = now
    elif previous is not None and not changed_owner and ownership_since is None:
        starts = {p.get("owner_start") for p in history if p["owner_id"] == owner
                  and p["rc_subscription_id"] == rc_id and p.get("owner_end") is None}
        if len(starts) > 1:
            raise EvidenceError("conflicting ownership start")
        if starts:
            ownership_since = instant(next(iter(starts)))
    if changed_owner:
        ownership_since = now
        for old in history:
            if old["owner_id"] == previous_owner and old.get("owner_end") is None:
                old["owner_end"] = now.isoformat()
    proofs: dict[str, list[dict]] = {}
    for event in events:
        if (event.get("type") in {"INITIAL_PURCHASE", "RENEWAL"}
                and str(event.get("environment", "")).upper() == environment
                and event.get("transaction_id") in txs):
            proofs.setdefault(event["transaction_id"], []).append(event)
    current_end = instant(snapshot.get("current_period_ends_at"))
    current_start = instant(snapshot.get("current_period_starts_at"))
    delta = snapshot.get("pending_changes")
    if delta is not None and not isinstance(delta, dict):
        raise EvidenceError("invalid pending changes")
    pending = {**snapshot, **(delta or {})}  # Preserve explicit false and null.
    next_tx = pending.get("store_subscription_identifier") if delta else None
    new_periods = []
    for tx_id, tx in txs.items():
        tx_proofs = proofs.get(tx_id, [])
        previous_kinds = {p.get("billing_kind", p["kind"]) for p in history
                          if p["transaction_id"] == tx_id and p["rc_subscription_id"] == rc_id}
        plan = _transaction_plan(tx.get("product_store_identifier"), products, store)
        if plan not in {"monthly", "yearly"}:
            raise EvidenceError("unmapped transaction product")
        start = instant(tx.get("purchased_at"))
        end = instant(tx.get("effective_expiration_date")) or instant(tx.get("expiration_date"))
        if tx_id == latest:
            start = current_start or start
            # Current period end is distinct from next period end.
            end = current_end or end
            if gives_access and snapshot.get("status") == "in_grace_period":
                grace_end = instant(tx.get("effective_expiration_date"))
                if grace_end is None or grace_end <= now:
                    billing = [e for e in events if e.get("type") == "BILLING_ISSUE"
                               and e.get("transaction_id") == tx_id
                               and str(e.get("environment", "")).upper() == environment]
                    last_billing = max(billing, key=lambda e: e.get("event_timestamp_ms") or 0) if billing else None
                    grace_end = instant(last_billing.get("grace_period_expiration_at_ms")) if last_billing else None
                if grace_end is None or grace_end <= now:
                    verified_grace = [instant(p["end"]) for p in history
                                      if p["transaction_id"] == tx_id and p["kind"] == "grace"
                                      and p["owner_id"] == owner and p.get("owner_end") is None]
                    grace_end = max((e for e in verified_grace if e and e > now), default=None)
                if grace_end is not None and grace_end > now:
                    end = grace_end
        if tx_id == next_tx and tx_id != latest:
            if pending.get("gives_access") is not True or pending.get("pending_payment") is not False:
                continue
            for key in ("current_period_starts_at", "current_period_ends_at"):
                if key in delta and instant(delta[key]) is None:
                    raise EvidenceError("next period bounds explicitly unknown")
            if "current_period_starts_at" in delta:
                pending_start = instant(delta["current_period_starts_at"])
                if start is not None and start != pending_start:
                    raise EvidenceError("next period start conflicts with transaction")
                start = pending_start
            if "current_period_ends_at" in delta:
                end = instant(delta["current_period_ends_at"])
        if end is None:
            proven_ends = {instant(p.get("expiration_at_ms")) for p in tx_proofs}
            proven_ends.discard(None)
            if len(proven_ends) == 1:
                end = next(iter(proven_ends))
        if start is None or end is None:
            raise EvidenceError("transaction period unavailable")
        paid = (_positive((tx.get("revenue_in_local_currency") or {}).get("gross"))
                or any(_positive(p.get("price_in_purchased_currency"))
                       and p.get("period_type") != "TRIAL" for p in tx_proofs)
                or "paid" in previous_kinds)
        trial = (tx_id == latest and snapshot.get("status") == "trialing") or any(
            p.get("period_type") == "TRIAL" for p in tx_proofs) or "trial" in previous_kinds
        if paid and trial:
            raise EvidenceError("conflicting trial and paid proof")
        if not paid and not trial:
            # A refunded paid period can have zero revenue. Do not turn it into a trial.
            if end <= now:
                continue
            raise EvidenceError("transaction period type unverified")
        if start > now:
            if not paid or not gives_access:
                continue
            if tx_id == next_tx and (pending.get("gives_access") is not True
                                     or pending.get("pending_payment") is not False):
                continue
            if tx_id != next_tx and not any(
                _positive(p.get("price_in_purchased_currency"))
                and p.get("period_type") != "TRIAL" for p in tx_proofs
            ):
                raise EvidenceError("future paid transaction not confirmed")
        terminal_events = [p for p in events if p.get("transaction_id") == tx_id
                           and str(p.get("environment", "")).upper() == environment
                           and (p.get("type") in {"EXPIRATION", "REFUND_REVERSED"}
                                or (p.get("type") == "CANCELLATION"
                                    and p.get("cancel_reason") == "CUSTOMER_SUPPORT"))]
        if terminal_events and (tx_id != latest or not gives_access):
            last_terminal = max(terminal_events, key=lambda p: p.get("event_timestamp_ms") or 0)
            if last_terminal.get("type") != "REFUND_REVERSED":
                terminated = instant(last_terminal.get("expiration_at_ms"))
                if terminated is not None:
                    end = min(end, terminated)
        if not gives_access:
            end = min(end, now)
        if end < start:
            continue  # Refunded before its start: no access interval.
        kind = "trial" if trial else "paid"
        if tx_id == latest and snapshot.get("status") == "in_grace_period":
            kind = "grace"
        new_periods.append({
            "transaction_id": tx_id, "rc_subscription_id": rc_id,
            "store": store, "environment": environment, "owner_id": owner,
            "owner_start": ownership_since.isoformat() if ownership_since else None,
            "owner_end": None, "start": start.isoformat(), "end": end.isoformat(),
            "plan": plan, "kind": kind, "source": "revenuecat_v2",
            "billing_kind": "trial" if trial else "paid",
        })
    replaced = {(p["rc_subscription_id"], p["transaction_id"], p["owner_id"]) for p in new_periods}
    # Keep other objects/owners, but replace ALL periods of this object's current
    # owner, including removed/refunded future periods. Otherwise stale rights survive.
    history = [p for p in history if p.get("owner_end") is not None
               or ((p["rc_subscription_id"] != rc_id or p["owner_id"] != owner)
                   and (p["rc_subscription_id"], p["transaction_id"], p["owner_id"]) not in replaced)]
    evidence = {
        "version": VERSION, "revision": previous["revision"] + 1 if previous else 1,
        "verified_at": now.isoformat(), "rc_subscription_id": rc_id, "owner_id": owner,
        "store": store, "environment": environment,
        "auto_renew_enabled": snapshot.get("auto_renewal_status") in {
            "will_renew", "will_change_product", "has_already_renewed"},
        "current_transaction_id": latest, "periods": history + new_periods,
        "legacy_history": deepcopy(previous.get("legacy_history", [])) if previous else [],
        "store_original_transaction_ids": sorted(set(
            previous.get("store_original_transaction_ids", []) if previous else []
        ) | {str(e["original_transaction_id"]) for e in events
             if e.get("transaction_id") in txs and e.get("original_transaction_id")
             and str(e.get("environment", "")).upper() == environment}),
    }
    validate(evidence)
    # A provider saying access exists without a usable current period is a conflict,
    # not a reason to revoke a customer or invent an unlimited grace interval.
    class Candidate:
        access_evidence = evidence
        user_id = owner
    if gives_access and resolve_access(Candidate(), now) is None:
        raise EvidenceError("current access period not proven")
    return evidence


def project(sub: Any, evidence: dict, at: datetime) -> None:
    """Update compatible columns without mutating a live ORM object while reading."""
    validate(evidence)
    sub.access_evidence = deepcopy(evidence)
    sub.auto_renew_enabled = evidence["auto_renew_enabled"]
    sub.rc_subscription_id = evidence["rc_subscription_id"]
    sub.latest_transaction_id = evidence["current_transaction_id"]
    sub.environment = evidence["environment"]
    current = resolve_access(sub, at)
    periods = [p for p in evidence["periods"] if p["owner_id"] == str(sub.user_id)
               and p.get("owner_end") is None]
    if current is None:
        sub.status = "expired"
        sub.store_trial_ends_at = None
        past = [p for p in periods if _required(p["start"], "start") <= at]
        if past:
            last = max(past, key=lambda p: _required(p["end"], "end"))
            sub.plan = last["plan"]
            sub.purchased_at = _required(last["start"], "start")
            sub.expires_at = _required(last["end"], "end")
        else:
            sub.expires_at = at
        return
    sub.status, sub.plan = current.status, current.plan
    sub.purchased_at = current.purchased_at
    sub.store_trial_ends_at = current.store_trial_ends_at
    horizon = current.expires_at
    for period in sorted(periods, key=lambda p: _required(p["start"], "start")):
        start, end = _required(period["start"], "start"), _required(period["end"], "end")
        if start <= horizon and end > horizon:
            horizon = end
    sub.expires_at = horizon
