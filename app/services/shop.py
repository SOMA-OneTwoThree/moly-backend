"""상점·꾸미기 — 상품·구매(건초 차감)·인벤토리·4슬롯 장착. 서버 권위."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.shop import ShopItem, UserItem
from app.models.user_equipment import UserEquipment
from app.services import gating, hay_ledger
from app.services.account import _uid

_SLOTS = ("background", "head", "neck", "body")


async def _owned_ids(session: AsyncSession, uid: uuid.UUID) -> set[uuid.UUID]:
    return set(
        (
            await session.execute(select(UserItem.shop_item_id).where(UserItem.user_id == uid))
        ).scalars().all()
    )


async def _equipped_map(session: AsyncSession, uid: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = (
        await session.execute(select(UserEquipment).where(UserEquipment.user_id == uid))
    ).scalars().all()
    return {e.slot: e.shop_item_id for e in rows}


async def get_products(session: AsyncSession, user_id: str) -> dict[str, Any]:
    g = await gating.resolve(session, user_id)
    unlocked = g.entitlement["subscriber_theme_unlocked"]
    uid = g.profile.id
    items = list(
        (
            await session.execute(
                select(ShopItem).where(ShopItem.is_active.is_(True)).order_by(ShopItem.sort_order)
            )
        ).scalars().all()
    )
    owned = await _owned_ids(session, uid)
    equipped = set((await _equipped_map(session, uid)).values())
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


async def _load_item(session: AsyncSession, item_id: str) -> ShopItem:
    try:
        iid = uuid.UUID(item_id)
    except ValueError as e:
        raise errors.AppError("NOT_FOUND", 404, "상품을 찾을 수 없어요.") from e
    it = await session.get(ShopItem, iid)
    if it is None or not it.is_active:
        raise errors.AppError("NOT_FOUND", 404, "상품을 찾을 수 없어요.")
    return it


async def purchase(session: AsyncSession, user_id: str, product_id: str) -> dict[str, Any]:
    uid = _uid(user_id)
    it = await _load_item(session, product_id)
    if it.is_subscriber_only or it.price_hay is None:
        raise errors.subscriber_only()  # 구독 전용 = 구매 대상 아님(잠금해제식)
    if it.id in await _owned_ids(session, uid):
        raise errors.already_owned()
    balance = await hay_ledger.apply(session, uid, "shop_purchase", -it.price_hay, ref_id=str(it.id))
    session.add(UserItem(user_id=uid, shop_item_id=it.id))
    await session.commit()
    return {"product_id": str(it.id), "price_hay": it.price_hay, "balance_after": balance}


async def get_inventory(session: AsyncSession, user_id: str) -> dict[str, Any]:
    uid = _uid(user_id)
    return {"data": [str(i) for i in await _owned_ids(session, uid)]}


async def get_equipment(session: AsyncSession, user_id: str) -> dict[str, Any]:
    uid = _uid(user_id)
    eq = await _equipped_map(session, uid)
    return {f"{slot}_id": (str(eq[slot]) if slot in eq else None) for slot in _SLOTS}


async def put_equipment(session: AsyncSession, user_id: str, req) -> dict[str, Any]:
    g = await gating.resolve(session, user_id)
    uid = g.profile.id
    unlocked = g.entitlement["subscriber_theme_unlocked"]
    owned = await _owned_ids(session, uid)
    for slot in _SLOTS:
        item_id = getattr(req, f"{slot}_id")
        if item_id is None:  # 해제 = 행 삭제
            await session.execute(
                delete(UserEquipment).where(
                    UserEquipment.user_id == uid, UserEquipment.slot == slot
                )
            )
            continue
        it = await _load_item(session, item_id)
        if it.slot != slot:
            raise errors.validation("슬롯이 맞지 않아요.", {"slot": slot})
        can_use = (it.id in owned) or (it.is_subscriber_only and unlocked)
        if not can_use:
            raise errors.not_owned()
        stmt = pg_insert(UserEquipment).values(user_id=uid, slot=slot, shop_item_id=it.id)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "slot"], set_={"shop_item_id": it.id}
        )
        await session.execute(stmt)
    await session.commit()
    return await get_equipment(session, user_id)
