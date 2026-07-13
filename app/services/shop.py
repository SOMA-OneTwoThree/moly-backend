"""상점·꾸미기 — 상품·구매(건초 차감)·인벤토리·4슬롯 장착. 서버 권위.

- 카탈로그 = products(cosmetic), 보유+장착 상태 = user_items.
- 구매: Order(HAY,paid) + OrderItem(가격 스냅샷) + 원장(order_id) + UserItem(source=purchase)
- 장착: equipped_slot 갱신(해제=NULL). 구독 전용은 source=subscription 행으로 장착(소유 아님).
- 인벤토리: source!=subscription 행만(구매·무상지급).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.product import Product
from app.models.user_item import UserItem
from app.services import gating, hay_ledger
from app.services import order as order_service
from app.services.account import _uid

_SLOTS = ("background", "head", "neck", "body")


async def _user_rows(session: AsyncSession, uid: uuid.UUID) -> list[UserItem]:
    return list(
        (
            await session.execute(select(UserItem).where(UserItem.user_id == uid))
        ).scalars().all()
    )


async def _owned_ids(session: AsyncSession, uid: uuid.UUID) -> set[uuid.UUID]:
    """보유(구매·무상지급) 상품 id. source=subscription(구독 전용 장착용 행)은 소유가 아님."""
    return {r.product_id for r in await _user_rows(session, uid) if r.source != "subscription"}


async def _equipped_map(session: AsyncSession, uid: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = await _user_rows(session, uid)
    return {r.equipped_slot: r.product_id for r in rows if r.equipped_slot is not None}


async def get_products(session: AsyncSession, user_id: str) -> dict[str, Any]:
    g = await gating.resolve(session, user_id)
    unlocked = g.entitlement["subscriber_theme_unlocked"]
    uid = g.profile.id
    items = list(
        (
            await session.execute(
                select(Product)
                .where(Product.product_type == "cosmetic", Product.is_active.is_(True))
                .order_by(Product.sort_order)
            )
        ).scalars().all()
    )
    rows = await _user_rows(session, uid)
    owned = {r.product_id for r in rows if r.source != "subscription"}
    equipped = {r.product_id for r in rows if r.equipped_slot is not None}
    backgrounds, other = [], []
    for it in items:
        dto: dict[str, Any] = {
            "id": str(it.id), "name": it.name, "slot": it.slot,
            "price_hay": it.price_hay, "is_subscriber_only": it.is_subscriber_only,
            "equipped": it.id in equipped, "assets": it.assets,
        }
        if it.is_subscriber_only:
            dto["unlocked"] = unlocked
        else:
            dto["owned"] = it.id in owned
        (backgrounds if it.slot == "background" else other).append(dto)
    return {"backgrounds": backgrounds, "items": other}


async def _load_item(session: AsyncSession, item_id: str) -> Product:
    try:
        iid = uuid.UUID(item_id)
    except ValueError as e:
        raise errors.AppError("NOT_FOUND", 404, "상품을 찾을 수 없어요.") from e
    it = await session.get(Product, iid)
    if it is None or not it.is_active or it.product_type != "cosmetic":
        raise errors.AppError("NOT_FOUND", 404, "상품을 찾을 수 없어요.")
    return it


async def purchase(session: AsyncSession, user_id: str, product_id: str) -> dict[str, Any]:
    uid = _uid(user_id)
    it = await _load_item(session, product_id)
    if it.is_subscriber_only or it.price_hay is None:
        raise errors.subscriber_only()  # 구독 전용 = 구매 대상 아님(잠금해제식)
    if it.id in await _owned_ids(session, uid):
        raise errors.already_owned()
    ord_ = order_service.create_paid_order(
        session, uid, currency="HAY", product=it, unit_price=it.price_hay
    )
    tx = await hay_ledger.apply(session, uid, "shop_purchase", -it.price_hay, order_id=ord_.id)
    session.add(UserItem(user_id=uid, product_id=it.id, source="purchase", order_id=ord_.id))
    try:
        await session.commit()
    except IntegrityError as e:
        # 동시 구매 레이스 — (user, product) UNIQUE 충돌. 차감 롤백 후 멱등 409(이중 차감 없음).
        await session.rollback()
        raise errors.already_owned() from e
    return {
        "product_id": str(it.id), "order_id": str(ord_.id),
        "price_hay": it.price_hay, "balance_after": tx.balance_after,
    }


async def get_inventory(session: AsyncSession, user_id: str) -> dict[str, Any]:
    uid = _uid(user_id)
    return {"data": [str(i) for i in await _owned_ids(session, uid)]}


async def get_equipment(session: AsyncSession, user_id: str) -> dict[str, Any]:
    uid = _uid(user_id)
    eq = await _equipped_map(session, uid)
    return {f"{slot}_id": (str(eq[slot]) if slot in eq else None) for slot in _SLOTS}


async def _unequip_row(session: AsyncSession, row: UserItem) -> None:
    """장착 해제. 구독 전용 장착용 행(source=subscription)은 존재 이유가 장착뿐 → 행 삭제."""
    if row.source == "subscription":
        await session.delete(row)
    else:
        row.equipped_slot = None
        row.equipped_at = None


async def put_equipment(session: AsyncSession, user_id: str, req) -> dict[str, Any]:
    g = await gating.resolve(session, user_id)
    uid = g.profile.id
    unlocked = g.entitlement["subscriber_theme_unlocked"]
    rows = await _user_rows(session, uid)
    by_product = {r.product_id: r for r in rows}
    by_slot = {r.equipped_slot: r for r in rows if r.equipped_slot is not None}
    now = datetime.now(timezone.utc)
    # 검증 선행 — 하나라도 실패하면 아무 변경 없이 에러(전체 교체 원자성)
    targets: list[tuple[str, Product | None]] = []
    for slot in _SLOTS:
        item_id = getattr(req, f"{slot}_id")
        if item_id is None:
            targets.append((slot, None))
            continue
        it = await _load_item(session, item_id)
        if it.slot != slot:
            raise errors.validation("슬롯이 맞지 않아요.", {"slot": slot})
        row = by_product.get(it.id)
        owned = row is not None and row.source != "subscription"
        if not (owned or (it.is_subscriber_only and unlocked)):
            raise errors.not_owned()
        targets.append((slot, it))
    # 1차 해제 → flush → 2차 장착. 슬롯당 1장착 부분 UNIQUE는 statement 단위 평가라
    # "해제·장착"이 한 flush에 섞이면 순서에 따라 위반 — 슬롯을 먼저 비워서 DB에 반영한다.
    to_equip: list[tuple[str, Product]] = []
    for slot, it in targets:
        current = by_slot.get(slot)
        if it is not None and current is not None and current.product_id == it.id:
            continue  # 이미 그 슬롯에 장착 중 — no-op
        if current is not None:
            await _unequip_row(session, current)  # 해제 요청 또는 같은 슬롯 교체(기존 자동 해제)
        if it is not None:
            to_equip.append((slot, it))
    await session.flush()
    for slot, it in to_equip:
        row = by_product.get(it.id)
        if row is not None:
            row.equipped_slot, row.equipped_at = slot, now
        else:  # 구독 전용(소유 행 없음) — 장착용 행 생성
            session.add(
                UserItem(
                    user_id=uid, product_id=it.id, source="subscription",
                    equipped_slot=slot, equipped_at=now,
                )
            )
    await session.commit()
    return await get_equipment(session, user_id)
