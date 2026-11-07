"""Staged subscription processing for explicitly adopted subscriptions.

Provider reads happen after releasing the inbox claim. Applying a prepared read
rechecks every baseline under ownership/user/row locks. Old financial stages are
never rerun just because access or a purchase bonus still needs verification.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
from app.services import privacy, revenuecat, subscription_access as access
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


# ─────────────────────────────────────────────────────────────
# 결제 직후 받은 현재 소유자 결제 — 과거 결제 재생성 금지 가드의 예외(docs/SUBSCRIPTIONS.md 구독 처리 문단의 취지 유지).
# A 묶음의 첫 구매가 결제 직후 이 사용자 이름으로 왔다 · B 이번 결제도 결제 직후 받은 현재 기간 거래다 ·
# C 그 사이 이전(TRANSFER) 흔적·다른 사용자 충돌이 없다. 셋 다 만족할 때만 처리 시각으로 적힌
# 소유 시작 때문에 막지 않는다. 익명 alias만으로는 판단하지 않는다.
# ─────────────────────────────────────────────────────────────
_LIVE_WINDOW_S = 3600.0
_CLOCK_SKEW_S = 60.0      # 시계 오차 여유. 구매 시각보다 이 정도 이른 생성·수신 시각은 허용한다.
_ORIGIN_LIMIT = 200       # 같은 원거래의 첫 구매 기록 상한. 넘으면 전부 확인 못 했으니 막는다.
_CHAIN_ID_LIMIT = 2000    # 같은 원거래 inbox 거래 ID 상한. 넘으면 막는다.


def _live_delivery(payload: dict, received_at) -> bool:
    """RC 이벤트 생성 시각(event_timestamp_ms)과 서버 수신 시각(received_at) 모두 구매 후 1시간 안."""
    purchased = access.instant(payload.get("purchased_at_ms"))
    created = access.instant(payload.get("event_timestamp_ms"))
    if purchased is None or created is None or received_at is None:
        return False
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return (-_CLOCK_SKEW_S <= (created - purchased).total_seconds() <= _LIVE_WINDOW_S
            and -_CLOCK_SKEW_S <= (received_at - purchased).total_seconds() <= _LIVE_WINDOW_S)


async def _chain_origin_rows(session, event: dict) -> list:
    """같은 원거래의 첫 구매(INITIAL_PURCHASE, transaction_id == original_transaction_id) 이벤트 행 전부(상태 무관)."""
    original = event.get("original_transaction_id")
    return list((await session.execute(select(RevenuecatEvent).where(
        RevenuecatEvent.payload["original_transaction_id"].astext == original,
        RevenuecatEvent.payload["transaction_id"].astext == original,
        RevenuecatEvent.payload["type"].astext == "INITIAL_PURCHASE",
    ).order_by(RevenuecatEvent.received_at).limit(_ORIGIN_LIMIT + 1))).scalars())


async def _chain_transaction_ids(session, event: dict, target) -> list[str] | None:
    """이 묶음에 속한 거래 ID 전부: 현재·원거래, RC 스냅샷 거래 목록, inbox의 같은 원거래 이벤트 거래.

    상한을 넘으면 None(전부 확인하지 못했으니 막는다).
    """
    original = event.get("original_transaction_id")
    ids = {event.get("transaction_id"), original}
    ids.update(tx.get("id") for tx in target.transactions)
    inbox = list((await session.execute(select(RevenuecatEvent.payload["transaction_id"].astext).where(
        RevenuecatEvent.payload["original_transaction_id"].astext == original,
    ).limit(_CHAIN_ID_LIMIT + 1))).scalars())
    if len(inbox) > _CHAIN_ID_LIMIT:
        return None
    ids.update(inbox)
    return sorted(value for value in ids if isinstance(value, str) and value)


async def _transfer_involving(session, owner: str, since) -> bool:
    """처리 상태와 무관하게(대기·실패 포함) 이 사용자가 오간 TRANSFER가 since(1일 여유) 이후에 있는가.

    시각 조건을 조회에 넣어 개수 상한 때문에 관련 기록이 빠지지 않게 한다. 시각을 모르면 막는다.
    """
    if since is None:
        return True
    floor = since - timedelta(days=1)
    hit = (await session.execute(select(RevenuecatEvent.event_id).where(
        RevenuecatEvent.payload["type"].astext == "TRANSFER",
        or_(RevenuecatEvent.payload["transferred_to"].contains([owner]),
            RevenuecatEvent.payload["transferred_from"].contains([owner])),
        or_(RevenuecatEvent.received_at.is_(None), RevenuecatEvent.received_at >= floor),
    ).limit(1))).scalar_one_or_none()
    return hit is not None


async def _chain_conflict(session, owner_id, chain_ids: list[str]) -> bool:
    """다른 사용자의 구독 행·결제 행이 이 묶음의 거래 ID를 갖고 있는가."""
    from sqlalchemy import String, cast
    evidence_text = cast(Subscription.access_evidence, String)
    sub_hit = (await session.execute(select(Subscription.id).where(
        Subscription.user_id != owner_id,
        or_(Subscription.original_transaction_id.in_(chain_ids),
            Subscription.latest_transaction_id.in_(chain_ids),
            *[evidence_text.contains(value) for value in chain_ids]),
    ).limit(1))).scalar_one_or_none()
    if sub_hit is not None:
        return True
    pay_hit = (await session.execute(select(Payment.id).where(
        Payment.user_id != owner_id, Payment.store_transaction_id.in_(chain_ids),
    ).limit(1))).scalar_one_or_none()
    return pay_hit is not None


async def _live_owner_purchase(session, event: dict, received_at, sub, target) -> bool:
    from app.services import subscription as old

    owner = str(sub.user_id)
    snapshot = target.snapshot
    # 기존 구독 행의 주인이 지금 사용자와 다르면(같은 처리에서 행을 옮긴 경우 포함) 막는다.
    if target.before is not None and str(target.before.get("user_id")) != owner:
        return False
    tx, original = event.get("transaction_id"), event.get("original_transaction_id")
    if (event.get("type") not in {"INITIAL_PURCHASE", "RENEWAL"}
            or event.get("is_family_share") is not False
            or not isinstance(tx, str) or not tx or not isinstance(original, str) or not original
            or event.get("app_user_id") != owner or snapshot.get("customer_id") != owner
            or snapshot.get("store_subscription_identifier") != tx
            or not _live_delivery(event, received_at)):
        return False
    amount, _ = old._event_money(event)
    if amount is None or amount <= 0:
        return False
    # A: 묶음의 첫 구매. 다른 사용자 이름의 첫 구매 기록이 하나라도 있거나 상한을 넘으면 막는다
    # (구매 복원·이전 의심). 지금 처리 중인 이벤트가 그 첫 구매면 그 자체가 증거다.
    rows = await _chain_origin_rows(session, event)
    if len(rows) > _ORIGIN_LIMIT or any(row.payload.get("app_user_id") != owner for row in rows):
        return False
    if event.get("type") == "INITIAL_PURCHASE" and tx == original:
        origin_payload, origin_received = event, received_at
    else:
        if not rows:
            return False
        qualifying = [row for row in rows
                      if row.status == "processed"
                      and row.payload.get("is_family_share") is False
                      and str(row.payload.get("environment", "")).upper() == str(event.get("environment", "")).upper()
                      and row.payload.get("store") == event.get("store")
                      and _live_delivery(row.payload, row.received_at)]
        if not qualifying:
            return False
        origin_payload, origin_received = qualifying[0].payload, qualifying[0].received_at
    if not _live_delivery(origin_payload, origin_received):
        return False
    # C: 첫 구매 이후 이전 흔적·다른 사용자 충돌 없음(apply의 _ownership_lock 아래에서 호출된다).
    if await _transfer_involving(session, owner, access.instant(origin_payload.get("purchased_at_ms"))):
        return False
    chain_ids = await _chain_transaction_ids(session, event, target)
    if chain_ids is None:
        return False
    return not await _chain_conflict(session, sub.user_id, chain_ids)


# ─────────────────────────────────────────────────────────────
# 탈퇴가 끝난 계정의 스토어 결제 — 기록할 앱 계정이 없다(구독 행은 정리 트리거, 결제 행은 CASCADE로 사라진다).
# 재시도해도 결과가 같으므로 결제 기록·첫 구독 건초를 "건너뜀"으로 끝낸다. 이 계정 하나만 관련됐다는
# 증거가 모두 있을 때만 그렇게 하고, 하나라도 모자라면 기존 재시도·수동 확인을 따른다.
# 탈퇴가 진행 중(deleting)이면 되돌려질 수 있으므로 판정하지 않고, 하루 안에서는 재시도 횟수를 쓰지 않는다.
# ─────────────────────────────────────────────────────────────
_DELETED = privacy.STATUS_DELETED
_DELETING = privacy.STATUS_DELETING
_DELETING_HOLD = timedelta(hours=24)
HOLD_KEY = "hold_attempt"  # 이번 시도를 횟수에서 빼라는 표시. finish_attempt가 읽고 지운다.
_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _other_account_named(payload_names, owner: uuid.UUID) -> bool:
    """이벤트 하나의 app_user_id가 이 계정이 아니거나, 별칭·최초 사용자에 다른 UUID가 있는가."""
    app_user, original_user, aliases = payload_names
    if _uuid(app_user) != owner:
        return True  # 익명 ID·다른 계정·이름 없는 이벤트. 일부러 막는다(잘못 건너뛰는 것보다 수동 확인이 낫다).
    aliases = [] if aliases is None else aliases
    if (not isinstance(original_user, (str, type(None))) or not isinstance(aliases, list)
            or not all(isinstance(name, str) for name in aliases)):
        return True  # 형식을 모르면 확인하지 못한 것으로 본다.
    return any(_uuid(name) not in {None, owner} for name in [original_user, *aliases])


async def _other_user_in_chain(session, original: str, owner: uuid.UUID) -> bool:
    """같은 원거래 inbox 이벤트(상태 무관) 중 다른 계정 흔적이 하나라도 있는가. 상한을 넘으면 있다고 본다."""
    payload = RevenuecatEvent.payload
    rows = list((await session.execute(select(
        # 이름 칸은 JSON 원형으로 읽는다. 문자로 바꿔 읽으면 객체·숫자가 형식 검사를 빠져나간다.
        payload["app_user_id"].astext, payload["original_app_user_id"], payload["aliases"],
    ).where(payload["original_transaction_id"].astext == original).limit(_CHAIN_ID_LIMIT + 1))).all())
    return len(rows) > _CHAIN_ID_LIMIT or any(_other_account_named(row, owner) for row in rows)


async def _deleted_owner_purchase(session, event: dict, prepared: Prepared) -> str | None:
    """`deleted`=건너뛰어도 됨, `deleting`=탈퇴 진행 중, None=판정 불가(기존 처리)."""
    tx, original = event.get("transaction_id"), event.get("original_transaction_id")
    if (event.get("type") not in {"INITIAL_PURCHASE", "RENEWAL"}
            or event.get("is_family_share") is not False
            or not isinstance(tx, str) or not tx or not isinstance(original, str) or not original
            or prepared.before):
        return None
    matches = [t for t in prepared.targets if tx in {item.get("id") for item in t.transactions}]
    if len(matches) != 1 or matches[0].before is not None:
        return None
    target = matches[0]
    owner = _uuid(target.snapshot.get("customer_id"))
    if owner is None or _other_account_named(
            (event.get("app_user_id"), event.get("original_app_user_id"), event.get("aliases")), owner):
        return None  # 이벤트 사용자가 다르거나 다른 앱 계정이 별칭으로 붙어 있다.
    if await session.get(Profile, owner) is not None:
        return None
    status = (await privacy.load_barrier(session, owner)).status
    if status == _DELETING:
        return _DELETING
    if status != _DELETED:
        return None
    if await _other_user_in_chain(session, original, owner):
        return None
    if await _transfer_involving(session, str(owner), _EPOCH):
        return None
    chain_ids = await _chain_transaction_ids(session, event, target)
    if chain_ids is None or await _chain_conflict(session, owner, chain_ids):
        return None
    return _DELETED



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
    hold = state.pop(HOLD_KEY, False) is True
    tries = dict(state["tries"])
    for key in (*_FINANCE, "access_state"):
        if hold and key in ("payment_record", "bonus"):
            continue  # 탈퇴 진행 중: 결과가 정해지기 전이라 횟수를 쓰지 않는다.
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


async def _finance(session, event: dict, state: dict, pay: Payment | None, sub, prepared: Prepared,
                   received_at=None):
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
        # New payment/bonus needs provider ownership and transaction proof.
        if sub is None and pay is None and prepared.targets:
            verdict = await _deleted_owner_purchase(session, event, prepared)
            if verdict == _DELETED:
                state["payment_record"] = state["bonus"] = "skipped"
                state["reason"] = "deleted account purchase: no payment record or bonus"
            elif verdict == _DELETING:
                state["reason"] = "account deletion in progress"
                if received_at is not None:  # 수신 시각을 모르면 기다릴 기한도 모르니 횟수를 쓴다.
                    seen = received_at if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
                    state[HOLD_KEY] = datetime.now(timezone.utc) - seen < _DELETING_HOLD
        return
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
    unknown_new_owner = target.before is None and target.snapshot.get("original_customer_id") != str(sub.user_id)
    predates_ownership = sub.ownership_changed_at and (
        start is None or stamp is None or start < sub.ownership_changed_at or stamp < sub.ownership_changed_at)
    if pay is None and uid != sub.user_id:
        state["payment_record"] = state["bonus"] = "skipped"
        state["reason"] = "past owner purchase: financial ownership preserved"
        return
    # 결제 직후 받은 현재 소유자 결제는 처리 시각으로 적힌 소유 시작 때문에 막지 않는다(처음 처리·재처리 모두).
    # 가드가 막을 때만 판정해 일반 결제 처리에 조회를 더하지 않는다.
    if (pay is None and (unknown_new_owner or (predates_ownership and not original_first_purchase))
            and not await _live_owner_purchase(session, event, received_at, sub, target)):
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
    await _finance(session, event, state, pay, event_sub, prepared,
                   received_at=getattr(row, "received_at", None))
    state["verified_revision"] = {str(t.before["id"]): t.evidence["revision"]
                                  for t in prepared.targets if t.before}
    row.processing_state = deepcopy(state)
    return old.HandlerResult(old.HANDLED_PENDING, state.get("reason"))
