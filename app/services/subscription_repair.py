"""One-subscription repair previews. No payment, balance or inbox mutation is allowed."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace
import uuid

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.advisory_lock import advisory_xact_lock
from app.models.hay_transaction import HayTransaction
from app.models.payment import Payment
from app.models.profile import Profile
from app.models.revenuecat_event import RevenuecatEvent
from app.models.subscription import Subscription
from app.models.subscription_hay_grant import SubscriptionHayGrant
from app.services import revenuecat, subscription_access as access, subscription_processing as processing

_WRITABLE = {"rc_subscription_id", "plan", "status", "latest_transaction_id", "purchased_at",
             "expires_at", "store_trial_ends_at", "auto_renew_enabled", "environment", "access_evidence"}


class RepairCommitUncertain(access.EvidenceError):
    """A database commit response was lost; inspect before retrying any repair."""


class RepairCommitted(access.EvidenceError):
    """The repair committed, but persisting its later audit failed."""


def serial(value):
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    if isinstance(value, (datetime, uuid.UUID, Decimal)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(serial(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


async def _financial_digest(session, sub) -> str:
    payments = (await session.execute(select(
        Payment.id, Payment.user_id, Payment.status, Payment.amount, Payment.currency,
        Payment.store_transaction_id, Payment.subscription_bonus_review, Payment.subscription_hay_grant_id,
    ).where(Payment.subscription_id == sub.id).order_by(Payment.id))).all()
    grants = (await session.execute(select(
        SubscriptionHayGrant.id, SubscriptionHayGrant.revoked_at,
        SubscriptionHayGrant.hay_transaction_id, SubscriptionHayGrant.clawback_hay_transaction_id,
    ).where(SubscriptionHayGrant.user_id == sub.user_id).order_by(SubscriptionHayGrant.id))).all()
    profile = await session.get(Profile, sub.user_id)
    ledger = (await session.execute(select(func.count(), func.max(HayTransaction.id), func.sum(HayTransaction.amount))
                                  .where(HayTransaction.user_id == sub.user_id))).one()
    return digest({"payments": [tuple(r) for r in payments], "grants": [tuple(r) for r in grants],
                   "hay": profile.hay_balance if profile else None, "ledger": tuple(ledger)})


async def _pending(session, sub, tx_ids: list[str]) -> bool:
    payload = RevenuecatEvent.payload
    return bool((await session.execute(select(RevenuecatEvent.event_id).where(
        RevenuecatEvent.status.in_(("pending", "failed")),
        or_(payload["transaction_id"].astext.in_(tx_ids),
            payload["original_transaction_id"].astext == sub.original_transaction_id,
            (payload["app_user_id"].astext == str(sub.user_id))
            & payload["type"].astext.in_(tuple(processing._TYPES)),
            (payload["type"].astext == "TRANSFER") & or_(
                payload["transferred_from"].contains([str(sub.user_id)]),
                payload["transferred_to"].contains([str(sub.user_id)]))),
    ).limit(1))).scalar_one_or_none())


async def _provider_proof(rc_id: str, owner: str) -> dict:
    snapshot = await revenuecat.get_subscription(rc_id)
    transactions = await revenuecat.subscription_transactions(rc_id)
    objects = await revenuecat.customer_subscriptions(owner)
    return {"snapshot": snapshot, "transactions": sorted(transactions, key=lambda t: t["id"]),
            "objects": sorted(objects, key=lambda s: s["id"])}


async def preview(session, sub_id: uuid.UUID, mode: str, *, project_ref: str, app_sha: str,
                  environment: str = "development") -> dict:
    """Build a reviewable plan using current provider proof, never local expiry alone."""
    if mode not in {"refunded", "metadata"}:
        raise access.EvidenceError("unsupported repair mode")
    if environment not in {"development", "production"}:
        raise access.EvidenceError("unsupported repair environment")
    sub = await session.get(Subscription, sub_id)
    if sub is None or sub.user_id is None or not sub.rc_subscription_id:
        raise access.EvidenceError("repair requires an owned, mapped subscription")
    before = processing.baseline(sub)
    pay = (await session.execute(select(Payment).where(Payment.subscription_id == sub.id)
                                 .order_by(Payment.paid_at.desc().nullslast()).limit(1))).scalars().first()
    if pay is None or (mode == "refunded" and pay.status != "refunded"):
        raise access.EvidenceError("repair payment state does not match mode")
    if mode == "metadata" and pay.status != "paid":
        raise access.EvidenceError("metadata repair requires an unrefunded payment")
    tx, owner, rc_id, payment_id = pay.store_transaction_id, str(sub.user_id), sub.rc_subscription_id, str(pay.id)
    prepared = processing.Prepared([before], None, datetime.now(timezone.utc))
    event = {"type": "REPAIR", "transaction_id": tx, "original_transaction_id": sub.original_transaction_id,
             "environment": sub.environment, "app_user_id": owner}
    # No mutation is pending; release the read transaction before external I/O.
    await session.rollback()
    prepared = await processing.fetch(session, event, prepared)
    if prepared.error or len(prepared.targets) != 1:
        raise access.EvidenceError(prepared.error or "repair object ambiguous")
    target = prepared.targets[0]
    if target.before != before or target.snapshot.get("customer_id") != owner or target.snapshot["id"] != rc_id:
        raise access.EvidenceError("repair ownership or baseline changed")
    proof = await _provider_proof(rc_id, owner)
    if proof["snapshot"] != target.snapshot or proof["transactions"] != sorted(target.transactions, key=lambda t: t["id"]):
        raise access.EvidenceError("provider changed while preparing repair")
    projected = SimpleNamespace(**deepcopy(before))
    now = datetime.now(timezone.utc)
    access.project(projected, target.evidence, now)
    after_access = access.resolve_access(projected, now)
    if mode == "refunded":
        if after_access is not None or target.snapshot.get("gives_access") is not False:
            raise access.EvidenceError("current refunded access is not terminal")
        for obj in proof["objects"]:
            if obj.get("id") == rc_id:
                continue
            end = access.instant(obj.get("ends_at"))
            start = access.instant(obj.get("starts_at")) or access.instant(obj.get("current_period_starts_at"))
            if (obj.get("gives_access") is not False or obj.get("pending_payment") is not False
                    or obj.get("pending_changes") is not None or obj.get("status") != "expired"
                    or end is None or end > now
                    or (start is not None and projected.expires_at is not None and start > projected.expires_at)):
                raise access.EvidenceError("new, scheduled or ambiguous store subscription requires re-planning")
    elif (after_access is None or access.resolve_access(SimpleNamespace(**before), now) is None
          or after_access.status != "active" or after_access.store_trial_ends_at is not None
          or target.evidence["current_transaction_id"] != tx
          or projected.expires_at != before["expires_at"]):
        raise access.EvidenceError("metadata repair must preserve current paid access and expiry")
    live = await session.get(Subscription, sub_id, populate_existing=True)
    tx_ids = [t["id"] for t in target.transactions]
    if live is None or processing.baseline(live) != before or await _pending(session, live, tx_ids):
        raise access.EvidenceError("subscription or related inbox requires re-planning")
    financial = await _financial_digest(session, live)
    result = {"version": 1, "environment": environment, "project_ref": project_ref,
              "app_sha": app_sha, "created_at": now.isoformat(), "mode": mode,
              "subscription_id": str(sub_id), "owner_id": owner, "tx_ids": tx_ids,
              "payment_id": payment_id, "payment_tx": tx,
              "before": serial(before), "after": serial({k: getattr(projected, k) for k in sorted(_WRITABLE)}),
              "provider_sha256": digest(proof), "financial_sha256": financial}
    await session.rollback()
    return result


async def apply_preview(session, plan: dict, *, expected_sha256: str, project_ref: str, app_sha: str,
                        on_status=None, environment: str = "development") -> dict:
    """Caller must enforce the explicitly confirmed target and compatible fleet."""
    from app.services import subscription as old

    if digest(plan) != expected_sha256 or plan.get("version") != 1:
        raise access.EvidenceError("repair preview checksum mismatch")
    if (environment not in {"development", "production"} or plan.get("environment") != environment
            or plan.get("project_ref") != project_ref or plan.get("app_sha") != app_sha):
        raise access.EvidenceError("repair target or deployed version mismatch")
    created = access.instant(plan.get("created_at"))
    age = (datetime.now(timezone.utc) - created).total_seconds() if created else -1
    if not 0 <= age < 60 or set(plan.get("after", {})) != _WRITABLE:
        raise access.EvidenceError("repair preview expired or fields changed")
    proof = await _provider_proof(plan["after"]["rc_subscription_id"], plan["owner_id"])
    if digest(proof) != plan["provider_sha256"]:
        raise access.EvidenceError("provider changed after preview")
    await session.rollback()
    await session.execute(text("SET LOCAL lock_timeout = '2s'"))
    await old._ownership_lock(session)
    await advisory_xact_lock(session, uuid.UUID(plan["owner_id"]))
    sub = await session.get(Subscription, uuid.UUID(plan["subscription_id"]), with_for_update=True, populate_existing=True)
    if sub is None or serial(processing.baseline(sub)) != plan["before"] or await _pending(session, sub, plan["tx_ids"]):
        raise access.EvidenceError("repair baseline changed")
    if await _financial_digest(session, sub) != plan["financial_sha256"]:
        raise access.EvidenceError("financial activity changed after preview")
    age = (datetime.now(timezone.utc) - created).total_seconds()
    if not 0 <= age < 60:
        raise access.EvidenceError("repair preview expired while reading provider or waiting for locks")
    for key, value in plan["after"].items():
        setattr(sub, key, access.instant(value) if key in {"purchased_at", "expires_at", "store_trial_ends_at"} else deepcopy(value))
    await session.flush()
    if await _financial_digest(session, sub) != plan["financial_sha256"]:
        raise access.EvidenceError("repair financial guard failed")
    after = serial(processing.baseline(sub))
    audit = {"version": 1, "status": "commit_pending", "plan_sha256": expected_sha256,
             "subscription_id": plan["subscription_id"], "before": plan["before"], "after": after}
    if on_status is not None:
        on_status(deepcopy(audit))  # Durable audit reservation must succeed BEFORE commit.
    try:
        await session.commit()
    except BaseException:
        audit["status"] = "commit_result_unknown"
        if on_status is not None:
            try:
                on_status(deepcopy(audit))
            except OSError:
                pass  # The previous commit_pending journal entry remains durable.
        raise RepairCommitUncertain("commit result unknown; inspect the reserved journal and current state") from None
    audit.update(status="committed", applied_at=datetime.now(timezone.utc).isoformat())
    if on_status is not None:
        try:
            on_status(deepcopy(audit))
        except OSError:
            raise RepairCommitted("repair committed; audit update failed; stop and inspect the reserved journal") from None
    # Never blindly restore a bad previous active state after a concurrent purchase.
    try:
        current = await _provider_proof(plan["after"]["rc_subscription_id"], plan["owner_id"])
        unchanged = digest(current) == plan["provider_sha256"]
        audit["postcheck"] = "verified" if unchanged else "provider_changed"
    except (revenuecat.RevenueCatError, TimeoutError):
        audit["postcheck"] = "provider_unavailable"
    try:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        current_sub = await session.get(Subscription, uuid.UUID(plan["subscription_id"]), populate_existing=True)
        if (current_sub is None or serial(processing.baseline(current_sub)) != after
                or await _pending(session, current_sub, plan["tx_ids"])):
            audit["postcheck"] = "local_activity_changed"
        elif await _financial_digest(session, current_sub) != plan["financial_sha256"]:
            audit["postcheck"] = "financial_activity_changed"
    except SQLAlchemyError:
        audit["postcheck"] = "database_unavailable"
    finally:
        await session.rollback()
    audit["postcheck_provider_unchanged"] = audit["postcheck"] == "verified"
    if on_status is not None:
        try:
            on_status(deepcopy(audit))
        except OSError:
            raise RepairCommitted("repair committed; postcheck audit update failed; stop and inspect the reserved journal") from None
    return audit
