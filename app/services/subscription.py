"""구독 — 상태·플랜·검증(증정)·복원·ASSN 웹훅. 서버가 영수증 검증·혜택 관리(서버 권위).

가격은 StoreKit이 원본. 증정 = 플랜별 최초 1회(월1000/연4000, DB UNIQUE 강제).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.hay_transaction import HayTransaction
from app.models.shop import ShopItem
from app.models.subscription import Subscription
from app.models.subscription_hay_grant import SubscriptionHayGrant
from app.models.user_equipment import UserEquipment
from app.services import app_store, hay_ledger
from app.services.account import _load_profile, _uid

_log = logging.getLogger("moly-backend")

_PLAN_BY_PRODUCT = {"app.moly.sub.monthly": "monthly", "app.moly.sub.yearly": "yearly"}
HAY_GRANT = {"monthly": 1000, "yearly": 4000}
_PLANS = [
    {"product_id": "app.moly.sub.monthly", "period": "monthly", "hay_grant": 1000},
    {"product_id": "app.moly.sub.yearly", "period": "yearly", "hay_grant": 4000},
]
_BENEFITS = ["대화 한도 확장", "개인 일기 발행", "배너 광고 제거", "구독 전용 배경", "건초 증정"]
_ACTIVE = ("active", "grace_period")


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
    in_trial = profile.trial_ends_at is not None and now < profile.trial_ends_at
    if sub is None:
        return {
            "status": "none", "plan": None, "auto_renew_enabled": False, "expires_at": None,
            "in_trial": in_trial, "trial_ends_at": _iso(profile.trial_ends_at) if in_trial else None,
        }
    return {
        "status": sub.status, "plan": sub.plan, "auto_renew_enabled": sub.auto_renew_enabled,
        "expires_at": _iso(sub.expires_at), "in_trial": in_trial,
        "trial_ends_at": _iso(profile.trial_ends_at) if in_trial else None,
    }


async def _by_original_tx(
    session: AsyncSession, original_tx: str, *, lock: bool = False
) -> Subscription | None:
    q = select(Subscription).where(Subscription.original_transaction_id == original_tx)
    if lock:  # 웹훅 동시처리 직렬화(REFUND 중복 clawback 레이스 방지)
        q = q.with_for_update()
    return (await session.execute(q)).scalars().first()


async def _clawback_done(session: AsyncSession, uid, original_tx: str) -> bool:
    """이 구독(original_tx)에 대한 회수 원장이 이미 있으면 True — 재생/재시도·REVOKE→REFUND 멱등."""
    row = await session.execute(
        select(HayTransaction.id).where(
            HayTransaction.user_id == uid,
            HayTransaction.type == "refund_revoke",
            HayTransaction.ref_id == original_tx,
        )
    )
    return row.scalars().first() is not None


async def _grant_exists(session: AsyncSession, uid, plan: str) -> bool:
    row = await session.execute(
        select(SubscriptionHayGrant).where(
            SubscriptionHayGrant.user_id == uid, SubscriptionHayGrant.plan == plan
        )
    )
    return row.scalars().first() is not None


async def _upsert_sub(session: AsyncSession, uid, payload: dict) -> Subscription:
    """JWS payload로 Subscription 생성/갱신 → 해당 행 반환. 다른 계정 소유면 409. verify·restore 공용."""
    plan = _PLAN_BY_PRODUCT.get(payload.get("productId"))
    if plan is None:
        raise errors.receipt_invalid()
    original_tx = str(payload.get("originalTransactionId") or payload.get("transactionId"))
    expires = _ms_to_dt(payload.get("expiresDate"))
    # 만료 거래 JWS(restore에 섞여올 수 있음)가 만료 구독을 active로 되살리지 않게 — 만료시각 기준 판정.
    status = "expired" if (expires is not None and expires <= datetime.now(timezone.utc)) else "active"
    sub = await _by_original_tx(session, original_tx)
    if sub is not None and sub.user_id != uid:
        raise errors.restore_conflict()
    # 환불/취소(revoked)는 종단 상태 — 아직 만료 안 된 서명거래 재전송으로 되살리지 못하게(무료혜택 방지).
    if sub is not None and sub.status == "revoked":
        return sub
    if sub is None:
        sub = Subscription(
            user_id=uid, plan=plan, status=status, original_transaction_id=original_tx,
            latest_transaction_id=str(payload.get("transactionId")), expires_at=expires,
            auto_renew_enabled=True, environment=payload.get("environment"),
        )
        session.add(sub)
    else:
        sub.plan, sub.status, sub.expires_at = plan, status, expires
        sub.latest_transaction_id = str(payload.get("transactionId"))
    return sub


async def verify(session: AsyncSession, user_id: str, signed_transaction: str) -> dict[str, Any]:
    uid = _uid(user_id)
    payload = app_store.decode_transaction(signed_transaction)
    sub = await _upsert_sub(session, uid, payload)
    plan, expires = sub.plan, sub.expires_at
    is_active = sub.status == "active"  # 만료 영수증=expired, revoked=종단 → 증정·응답에서 제외

    # 증정 = (user, plan) 최초 1회 — 활성 구독일 때만(만료·환불 영수증엔 미지급).
    granted = 0
    profile = await _load_profile(session, user_id)
    balance = profile.hay_balance
    if is_active and not await _grant_exists(session, uid, plan):
        granted = HAY_GRANT[plan]
        balance = await hay_ledger.apply(session, uid, "subscription_grant", granted)
        session.add(SubscriptionHayGrant(user_id=uid, plan=plan))
    try:
        await session.commit()
    except IntegrityError as e:
        # 동시 verify 레이스 — (user, plan) 증정 UNIQUE 충돌. 롤백 후 멱등 409(이중 증정 차단).
        await session.rollback()
        raise errors.already_processed() from e
    return {
        "status": sub.status, "plan": plan, "expires_at": _iso(expires),
        "hay_granted": granted, "balance_after": balance,
    }


async def restore(session: AsyncSession, user_id: str, signed_transactions: list[str]) -> dict[str, Any]:
    uid = _uid(user_id)
    for jws in signed_transactions:  # 각 거래로 구독 재활성(웹훅 유실 대비) + 충돌 검사
        await _upsert_sub(session, uid, app_store.decode_transaction(jws))
    try:
        await session.commit()
    except IntegrityError:
        # 동시 restore/verify 또는 배열 내 중복 JWS로 original_tx UNIQUE 충돌 — 롤백 후 현재 상태 반환(멱등).
        await session.rollback()
    return await get_subscription(session, user_id)


async def handle_webhook(session: AsyncSession, signed_payload: str) -> None:
    """ASSN v2 — 갱신·해지·환불 상태 동기. 상태 갱신 + 환불 시 증정 회수.

    서명검증 = app_store.decode_notification/decode_transaction(x5c). signedTransactionInfo 기반
    (signedRenewalInfo 세부 필드는 필요 시 확장).
    """
    payload = app_store.decode_notification(signed_payload)
    ntype = payload.get("notificationType")
    subtype = payload.get("subtype")
    tx_info = payload.get("data", {}).get("signedTransactionInfo")
    if not tx_info:
        return
    tx = app_store.decode_transaction(tx_info)
    original_tx = str(tx.get("originalTransactionId") or tx.get("transactionId"))
    sub = await _by_original_tx(session, original_tx, lock=True)  # 동시/중복 알림 직렬화
    if sub is None:
        return
    if ntype == "DID_RENEW":
        sub.status = "active"
        sub.expires_at = _ms_to_dt(tx.get("expiresDate"))
    elif ntype == "DID_FAIL_TO_RENEW" and subtype == "GRACE_PERIOD":
        sub.status = "grace_period"  # 유예 기간 — 혜택 유지(장착 해제 안 함)
    elif ntype in ("EXPIRED", "DID_FAIL_TO_RENEW", "GRACE_PERIOD_EXPIRED"):
        sub.status = "expired"
        await _unequip_subscriber_only(session, sub.user_id)  # 만료 → 전용 장착 해제
    elif ntype == "DID_CHANGE_RENEWAL_STATUS":
        sub.auto_renew_enabled = subtype == "AUTO_RENEW_ENABLED"  # 자동갱신 on/off만 반영
    elif ntype == "REVOKE":
        # 가족 공유 취소 — 혜택 회수(증정 건초는 유지, 환불 아님)
        sub.status = "revoked"
        await _unequip_subscriber_only(session, sub.user_id)
    elif ntype == "REFUND":
        sub.status = "revoked"
        await _unequip_subscriber_only(session, sub.user_id)  # 환불 → 전용 장착 해제(ERD §4.9)
        # 증정 건초 회수 — 원장(ref_id=original_tx)로 멱등: 재생·재시도·REVOKE→REFUND 모두 1회만.
        # 회수액은 환불된 거래의 플랜 기준(current sub.plan 아님 — 업/다운그레이드 후 오회수 방지).
        if not await _clawback_done(session, sub.user_id, original_tx):
            refunded_plan = _PLAN_BY_PRODUCT.get(tx.get("productId"), sub.plan)
            profile = await _load_profile(session, str(sub.user_id))
            clawback = min(HAY_GRANT.get(refunded_plan, 0), profile.hay_balance)  # 잔액 하한 0
            # 금액 0이어도 기록 — 이 원장 행이 멱등 마커(잔액 소진 후 재생 시 재회수 방지).
            await hay_ledger.apply(
                session, sub.user_id, "refund_revoke", -clawback, ref_id=original_tx
            )
    await session.commit()


async def _unequip_subscriber_only(session: AsyncSession, user_id) -> None:
    """구독 전용 아이템 장착 행 삭제 → 기본 복귀(ERD §4.9). 만료/환불 시."""
    subscriber_items = select(ShopItem.id).where(ShopItem.is_subscriber_only.is_(True))
    await session.execute(
        delete(UserEquipment).where(
            UserEquipment.user_id == user_id,
            UserEquipment.shop_item_id.in_(subscriber_items),
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
    - CANCELLATION: cancel_reason=CUSTOMER_SUPPORT(환불) → revoked+장착해제+증정 회수 /
                    그 외(UNSUBSCRIBE 등) → 자동갱신만 off(만료 전까지 혜택 유지)
    - EXPIRATION → expired+장착해제 / BILLING_ISSUE → grace_period
    멱등: original_transaction_id 행잠금 + (user,plan) 증정 UNIQUE + clawback ref_id 원장.
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
            sub = Subscription(
                user_id=uid, plan=plan, status="active", original_transaction_id=original_tx,
                latest_transaction_id=str(event.get("transaction_id")), expires_at=expires,
                auto_renew_enabled=True, environment=event.get("environment"),
            )
            session.add(sub)
        else:
            sub.plan, sub.status, sub.expires_at = plan, "active", expires
            sub.auto_renew_enabled = True
            sub.latest_transaction_id = str(event.get("transaction_id"))
        # 증정 = (user, plan) 최초 1회
        if not await _grant_exists(session, uid, plan):
            await hay_ledger.apply(session, uid, "subscription_grant", HAY_GRANT[plan])
            session.add(SubscriptionHayGrant(user_id=uid, plan=plan))

    elif etype == "CANCELLATION":
        sub = await _by_original_tx(session, original_tx, lock=True)
        if sub is None:
            return
        if event.get("cancel_reason") == "CUSTOMER_SUPPORT":  # 환불
            if sub.status != "revoked":
                sub.status = "revoked"
                await _unequip_subscriber_only(session, sub.user_id)
            if not await _clawback_done(session, sub.user_id, original_tx):
                refunded_plan = _PLAN_BY_PRODUCT.get(product_id, sub.plan)
                profile = await _load_profile(session, str(sub.user_id))
                clawback = min(HAY_GRANT.get(refunded_plan, 0), profile.hay_balance)
                await hay_ledger.apply(
                    session, sub.user_id, "refund_revoke", -clawback, ref_id=original_tx
                )
        else:  # 자동갱신 해제 — 만료 전까지 혜택 유지
            sub.auto_renew_enabled = False

    elif etype == "EXPIRATION":
        sub = await _by_original_tx(session, original_tx, lock=True)
        if sub is not None and sub.status != "revoked":
            sub.status = "expired"
            await _unequip_subscriber_only(session, sub.user_id)

    elif etype == "BILLING_ISSUE":
        sub = await _by_original_tx(session, original_tx, lock=True)
        if sub is not None and sub.status != "revoked":
            sub.status = "grace_period"  # 유예 — 혜택 유지

    else:
        # SUBSCRIPTION_PAUSED(만료 시 처리)·TRANSFER·NON_RENEWING_PURCHASE(건초 IAP는 별도)·
        # TEST·paywall 이벤트 등은 구독 상태에 영향 없음 → 무시.
        _log.info("RC 웹훅: 미처리 이벤트 %s", etype)
        return

    try:
        await session.commit()
    except IntegrityError:
        # 동시 증정 UNIQUE 충돌 등 — 롤백(멱등, 이미 처리됨).
        await session.rollback()
