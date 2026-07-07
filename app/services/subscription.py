"""구독 — 상태·플랜·검증(증정)·복원·ASSN 웹훅. 서버가 영수증 검증·혜택 관리(서버 권위).

가격은 StoreKit이 원본. 증정 = 플랜별 최초 1회(월1000/연4000, DB UNIQUE 강제).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.shop import ShopItem
from app.models.subscription import Subscription
from app.models.subscription_hay_grant import SubscriptionHayGrant
from app.models.user_equipment import UserEquipment
from app.services import app_store, hay_ledger
from app.services.account import _load_profile, _uid

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


async def _by_original_tx(session: AsyncSession, original_tx: str) -> Subscription | None:
    return (
        await session.execute(
            select(Subscription).where(Subscription.original_transaction_id == original_tx)
        )
    ).scalars().first()


async def _grant_exists(session: AsyncSession, uid, plan: str) -> bool:
    row = await session.execute(
        select(SubscriptionHayGrant).where(
            SubscriptionHayGrant.user_id == uid, SubscriptionHayGrant.plan == plan
        )
    )
    return row.scalars().first() is not None


async def _upsert_sub(session: AsyncSession, uid, payload: dict) -> str:
    """JWS payload로 Subscription 생성/갱신 → plan 반환. 다른 계정 소유면 409. verify·restore 공용."""
    plan = _PLAN_BY_PRODUCT.get(payload.get("productId"))
    if plan is None:
        raise errors.receipt_invalid()
    original_tx = str(payload.get("originalTransactionId") or payload.get("transactionId"))
    expires = _ms_to_dt(payload.get("expiresDate"))
    sub = await _by_original_tx(session, original_tx)
    if sub is not None and sub.user_id != uid:
        raise errors.restore_conflict()
    if sub is None:
        session.add(
            Subscription(
                user_id=uid, plan=plan, status="active", original_transaction_id=original_tx,
                latest_transaction_id=str(payload.get("transactionId")), expires_at=expires,
                auto_renew_enabled=True, environment=payload.get("environment"),
            )
        )
    else:
        sub.plan, sub.status, sub.expires_at = plan, "active", expires
        sub.latest_transaction_id = str(payload.get("transactionId"))
    return plan


async def verify(session: AsyncSession, user_id: str, signed_transaction: str) -> dict[str, Any]:
    uid = _uid(user_id)
    payload = app_store.decode(signed_transaction)
    plan = await _upsert_sub(session, uid, payload)
    expires = _ms_to_dt(payload.get("expiresDate"))

    # 증정 = (user, plan) 최초 1회
    granted = 0
    profile = await _load_profile(session, user_id)
    if not await _grant_exists(session, uid, plan):
        granted = HAY_GRANT[plan]
        balance = await hay_ledger.apply(session, uid, "subscription_grant", granted)
        session.add(SubscriptionHayGrant(user_id=uid, plan=plan))
    else:
        balance = profile.hay_balance
    await session.commit()
    return {
        "status": "active", "plan": plan, "expires_at": _iso(expires),
        "hay_granted": granted, "balance_after": balance,
    }


async def restore(session: AsyncSession, user_id: str, signed_transactions: list[str]) -> dict[str, Any]:
    uid = _uid(user_id)
    for jws in signed_transactions:  # 각 거래로 구독 재활성(웹훅 유실 대비) + 충돌 검사
        await _upsert_sub(session, uid, app_store.decode(jws))
    await session.commit()
    return await get_subscription(session, user_id)


async def handle_webhook(session: AsyncSession, signed_payload: str) -> None:
    """ASSN v2 — 갱신·해지·환불 상태 동기. MVP: 상태 갱신 + 환불 시 증정 회수.

    TODO: 서명검증 + signedRenewalInfo 등 전 필드 처리. 지금은 signedTransactionInfo 기반.
    """
    payload = app_store.decode(signed_payload)
    ntype = payload.get("notificationType")
    tx_info = payload.get("data", {}).get("signedTransactionInfo")
    if not tx_info:
        return
    tx = app_store.decode(tx_info)
    original_tx = str(tx.get("originalTransactionId") or tx.get("transactionId"))
    sub = await _by_original_tx(session, original_tx)
    if sub is None:
        return
    if ntype == "DID_RENEW":
        sub.status = "active"
        sub.expires_at = _ms_to_dt(tx.get("expiresDate"))
    elif ntype in ("EXPIRED", "DID_FAIL_TO_RENEW"):
        sub.status = "expired"
        await _unequip_subscriber_only(session, sub.user_id)  # 구독 만료 → 전용 장착 해제
    elif ntype == "REFUND":
        sub.status = "revoked"
        await _unequip_subscriber_only(session, sub.user_id)  # 환불 → 전용 장착 해제(ERD §4.9)
        # 증정 건초 회수(회수액 = min(증정량, 잔액), 잔액 하한 0)
        profile = await _load_profile(session, str(sub.user_id))
        clawback = min(HAY_GRANT.get(sub.plan, 0), profile.hay_balance)
        if clawback > 0:
            await hay_ledger.apply(session, sub.user_id, "refund_revoke", -clawback)
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
