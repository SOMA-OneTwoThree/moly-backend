"""구독 — RevenueCat이 진실 소스. 상태 조회 + RC 웹훅으로 상태·혜택 동기(서버 권위).

RC가 Apple/Google 영수증 검증을 대행 → 우리는 RC 웹훅 이벤트만 신뢰(서명 대신 웹훅 인증).
증정 = 플랜별 최초 1회(월1000/연4000, DB UNIQUE 강제). 건초 IAP = NON_RENEWING_PURCHASE.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment
from app.models.subscription import Subscription
from app.models.subscription_hay_grant import SubscriptionHayGrant
from app.services import hay_ledger, payment
from app.services.account import _load_profile
from app.services.entitlement import _parse_dt
from app.services.limits import effective_token_config

_log = logging.getLogger("moly-backend")

_PLAN_BY_PRODUCT = {"app.moly.sub.monthly": "monthly", "app.moly.sub.yearly": "yearly"}
HAY_GRANT = {"monthly": 1000, "yearly": 4000}
_PLANS = [
    {"product_id": "app.moly.sub.monthly", "period": "monthly", "hay_grant": 1000},
    {"product_id": "app.moly.sub.yearly", "period": "yearly", "hay_grant": 4000},
]
_BENEFITS = ["대화 한도 확장", "개인 일기 발행", "배너 광고 제거", "건초 증정"]
_ACTIVE = ("active", "grace_period")  # 혜택 유지되는 구독 상태


def _ms_to_dt(ms) -> datetime | None:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def get_plans() -> dict[str, Any]:
    return {"plans": _PLANS, "benefits": _BENEFITS}


async def _latest_sub(session: AsyncSession, uid) -> Subscription | None:
    return (
        await session.execute(
            select(Subscription).where(Subscription.user_id == uid).order_by(
                Subscription.expires_at.desc().nullslast()
            ).limit(1)
        )
    ).scalars().first()


async def get_subscription(session: AsyncSession, user_id: str) -> dict[str, Any]:
    profile = await _load_profile(session, user_id)
    sub = await _latest_sub(session, profile.id)
    now = datetime.now(timezone.utc)
    # 무료 체험 표시(entitlement와 일관): 활성 구독자=체험 아님 / 런칭 기간=런칭 종료까지 / 아니면 실제 2일.
    active = sub is not None and sub.status in _ACTIVE and sub.expires_at is not None and sub.expires_at > now
    launch_until = _parse_dt((await effective_token_config(session)).get("free_launch_until"))
    if active:
        in_trial, trial_ends = False, None
    elif launch_until is not None and now < launch_until:
        in_trial, trial_ends = True, launch_until
    elif profile.trial_ends_at is not None and now < profile.trial_ends_at:
        in_trial, trial_ends = True, profile.trial_ends_at
    else:
        in_trial, trial_ends = False, None
    if sub is None:
        return {
            "status": "none", "plan": None, "auto_renew_enabled": False, "expires_at": None,
            "in_trial": in_trial, "trial_ends_at": _iso(trial_ends),
        }
    return {
        "status": sub.status, "plan": sub.plan, "auto_renew_enabled": sub.auto_renew_enabled,
        "expires_at": _iso(sub.expires_at), "in_trial": in_trial, "trial_ends_at": _iso(trial_ends),
    }


async def _by_original_tx(
    session: AsyncSession, original_tx: str, *, lock: bool = False
) -> Subscription | None:
    q = select(Subscription).where(Subscription.original_transaction_id == original_tx)
    if lock:  # 웹훅 동시처리 직렬화(REFUND 중복 clawback 레이스 방지)
        q = q.with_for_update()
    return (await session.execute(q)).scalars().first()


async def _grant_exists(session: AsyncSession, uid, plan: str) -> bool:
    row = await session.execute(
        select(SubscriptionHayGrant).where(
            SubscriptionHayGrant.user_id == uid, SubscriptionHayGrant.plan == plan
        )
    )
    return row.scalars().first() is not None


async def _revoke_grant_with_clawback(session: AsyncSession, sub, refunded_plan: str) -> None:
    """환불 시 증정 건초 회수 — 멱등 = grants.revoked_at(회수 완료 표식).

    증정 이력이 없으면 회수할 것도 없음(받은 적 없는 건초를 뺏지 않는다).
    회수액 = min(증정량, 현재 잔액) — 잔액 하한 0. 0이면 원장 기록 없이 표식만.
    """
    grant = (
        await session.execute(
            select(SubscriptionHayGrant)
            .where(
                SubscriptionHayGrant.user_id == sub.user_id,
                SubscriptionHayGrant.plan == refunded_plan,
            )
            .with_for_update()
        )
    ).scalars().first()
    if grant is None or grant.revoked_at is not None:
        return  # 증정 없음 또는 이미 회수 — 멱등
    profile = await _load_profile(session, str(sub.user_id))
    clawback = min(HAY_GRANT.get(refunded_plan, 0), profile.hay_balance)
    tx = None
    if clawback > 0:
        tx = await hay_ledger.apply(session, sub.user_id, "refund_revoke", -clawback)
    grant.revoked_at = datetime.now(timezone.utc)
    grant.clawback_hay_transaction_id = tx.id if tx is not None else None


async def _record_subscription_payment(session: AsyncSession, sub, event: dict) -> None:
    """구독 결제(구매·갱신) payments 기록 — 매출 단일 소스(DB_REFACTOR §B.3). transaction_id 멱등."""
    tx_id = str(event.get("transaction_id") or "")
    if not tx_id or await payment.payment_exists(session, tx_id):
        return
    price = event.get("price_in_purchased_currency")
    try:
        amount = int(round(float(price))) if price is not None else None
    except (TypeError, ValueError):
        amount = None
    session.add(
        Payment(
            user_id=sub.user_id, subscription_id=sub.id, store_transaction_id=tx_id,
            amount=amount, currency=str(event.get("currency") or "KRW"), status="paid",
            paid_at=_ms_to_dt(event.get("purchased_at_ms")) or datetime.now(timezone.utc),
        )
    )


# RevenueCat을 구독 진실 소스로 쓸 때 상태를 active로 갱신하는 이벤트(문서 기준).
_RC_ACTIVE = frozenset(
    {"INITIAL_PURCHASE", "RENEWAL", "UNCANCELLATION", "PRODUCT_CHANGE",
     "SUBSCRIPTION_EXTENDED", "REFUND_REVERSED"}
)


async def handle_revenuecat_event(session: AsyncSession, event: dict) -> None:
    """RevenueCat 웹훅 이벤트 → 구독 상태·혜택 동기(서버 권위). 엔드포인트가 인증 후 호출.

    매핑(RC 공식 이벤트 기준):
    - 활성계열(구매·갱신·해지취소·상품변경·연장·환불복구) → active + 만료 갱신, 최초1회 증정
    - CANCELLATION: cancel_reason=CUSTOMER_SUPPORT(환불) → revoked+증정 회수 /
                    그 외(UNSUBSCRIBE 등) → 자동갱신만 off(만료 전까지 혜택 유지)
    - EXPIRATION → expired / BILLING_ISSUE → grace_period
    - NON_RENEWING_PURCHASE → 건초 IAP 지급(transaction_id 멱등)
    멱등: original_transaction_id 행잠금 + (user,plan) 증정 UNIQUE + clawback은 grants.revoked_at
    + 결제 기록은 payments.store_transaction_id UNIQUE.
    ⚠️ app_user_id = 우리 Supabase user_id 전제(클라가 RC logIn을 우리 uid로 해야 함).
    """
    etype = event.get("type")
    try:
        uid = uuid.UUID(str(event.get("app_user_id")))
    except (ValueError, TypeError):
        _log.warning("RC 웹훅: app_user_id 매핑 불가(%r) — 스킵", event.get("app_user_id"))
        return

    product_id = event.get("product_id")
    plan = _PLAN_BY_PRODUCT.get(product_id)
    original_tx = str(event.get("original_transaction_id") or event.get("transaction_id") or "")
    expires = _ms_to_dt(event.get("expiration_at_ms"))

    if etype in _RC_ACTIVE:
        if plan is None or not original_tx:
            _log.info("RC 웹훅: 미지원 상품/거래 없음(%s, %s) — 스킵", etype, product_id)
            return
        sub = await _by_original_tx(session, original_tx, lock=True)
        if sub is not None and sub.user_id != uid:
            _log.warning("RC 웹훅: 다른 계정 소유 구독(%s) — 스킵", original_tx)
            return
        if sub is not None and sub.status == "revoked":
            return  # 환불/취소는 종단 — 되살리지 않음
        if sub is None:
            # id 명시 생성 — payments.subscription_id가 flush 전에 참조(컬럼 default는 flush 시점)
            sub = Subscription(
                id=uuid.uuid4(),
                user_id=uid, plan=plan, status="active", original_transaction_id=original_tx,
                latest_transaction_id=str(event.get("transaction_id")), expires_at=expires,
                auto_renew_enabled=True, environment=event.get("environment"),
            )
            session.add(sub)
        else:
            sub.plan, sub.status, sub.expires_at = plan, "active", expires
            sub.auto_renew_enabled = True
            sub.latest_transaction_id = str(event.get("transaction_id"))
        # 결제 기록(구매·갱신) — 매출은 payments 단일 소스
        await _record_subscription_payment(session, sub, event)
        # 증정 = (user, plan) 최초 1회
        if not await _grant_exists(session, uid, plan):
            tx = await hay_ledger.apply(session, uid, "subscription_grant", HAY_GRANT[plan])
            session.add(SubscriptionHayGrant(user_id=uid, plan=plan, hay_transaction_id=tx.id))

    elif etype == "CANCELLATION":
        sub = await _by_original_tx(session, original_tx, lock=True)
        if sub is None:
            return
        if event.get("cancel_reason") == "CUSTOMER_SUPPORT":  # 환불
            if sub.status != "revoked":
                sub.status = "revoked"
            refunded_plan = _PLAN_BY_PRODUCT.get(product_id, sub.plan)
            await _revoke_grant_with_clawback(session, sub, refunded_plan)
        else:  # 자동갱신 해제 — 만료 전까지 혜택 유지
            sub.auto_renew_enabled = False

    elif etype == "EXPIRATION":
        sub = await _by_original_tx(session, original_tx, lock=True)
        if sub is not None and sub.status != "revoked":
            sub.status = "expired"

    elif etype == "BILLING_ISSUE":
        sub = await _by_original_tx(session, original_tx, lock=True)
        if sub is not None and sub.status != "revoked":
            sub.status = "grace_period"  # 유예 — 혜택 유지

    elif etype == "NON_RENEWING_PURCHASE":  # 건초 IAP(소비성)
        await payment.grant_pack(session, uid, product_id, str(event.get("transaction_id") or ""))

    else:
        # SUBSCRIPTION_PAUSED(만료 시 처리)·TRANSFER·TEST·paywall 등은 무시.
        _log.info("RC 웹훅: 미처리 이벤트 %s", etype)
        return

    try:
        await session.commit()
    except IntegrityError:
        # 동시 증정/IAP UNIQUE 충돌 등 — 롤백(멱등, 이미 처리됨).
        await session.rollback()
