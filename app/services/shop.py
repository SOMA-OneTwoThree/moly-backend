"""상점·꾸미기 — 문자열 공개 ID, 보유 기반 장착, 서버 권위 카탈로그."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.idempotency_key import IdempotencyKey, SHOP_PURCHASE_KEY_PREFIX
from app.models.product import Product
from app.models.profile import Profile
from app.models.user_item import UserItem
from app.schemas.shop import (
    EquipmentResponse,
    EquipmentResponseV2,
    PurchaseResponse,
    ShopProduct,
    ShopProductV2,
)
from app.services import hay_ledger
from app.services import order as order_service
from app.services.account import _uid

_SLOTS_V2 = ("theme", "hat", "glasses", "neck", "body")

_log = logging.getLogger("moly-backend")


def legacy_asset_view(assets: dict[str, Any]) -> dict[str, Any]:
    """레거시(구버전) 응답 — 새 자세 키를 숨겨 기존 계약 형태를 그대로 유지한다."""
    return {key: value for key, value in assets.items() if key != "rightside"}


def rightside_asset_view(assets: dict[str, Any]) -> dict[str, Any]:
    """v2(새 자세) 응답 — 착용 아이템은 rightside upright 레이어와 thumbnail만 노출한다.

    테마는 자세와 무관하므로 기존 형태(thumbnail·detail·scene)를 그대로 쓴다.
    """
    if assets.get("scene") is not None:
        return legacy_asset_view(assets)
    rightside = assets.get("rightside") or {}
    return {
        "thumbnail_url": assets["thumbnail_url"],
        "upright_layer_url": rightside.get("upright_layer_url"),
    }


def _legacy_slot(slot: str | None) -> str | None:
    """hat/glasses는 구버전 계약에서 단일 head 슬롯으로 투영한다."""
    return "head" if slot in ("hat", "glasses") else slot


def _equipped_product_ids(rows: list[UserItem], *, v2: bool) -> set[uuid.UUID]:
    by_slot = {row.equipped_slot: row.product_id for row in rows if row.equipped_slot is not None}
    if v2:
        return set(by_slot.values())
    # 레거시: hat/glasses 동시 장착도 head 슬롯 하나로 투영 — hat 우선, 탈락한 쪽은 미장착 처리.
    head = by_slot.get("hat") or by_slot.get("glasses")
    ids = {pid for slot, pid in by_slot.items() if slot not in ("hat", "glasses")}
    if head is not None:
        ids.add(head)
    return ids


async def _user_rows(session: AsyncSession, uid: uuid.UUID) -> list[UserItem]:
    return list(
        (
            await session.execute(select(UserItem).where(UserItem.user_id == uid))
        ).scalars().all()
    )


async def _owned_ids(session: AsyncSession, uid: uuid.UUID) -> set[uuid.UUID]:
    """구매·무상 지급 소유권. 구형 subscription 행은 소유권으로 인정하지 않는다."""
    return {r.product_id for r in await _user_rows(session, uid) if r.source != "subscription"}


async def _products_by_ids(
    session: AsyncSession, product_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Product]:
    if not product_ids:
        return {}
    products = list(
        (
            await session.execute(
                select(Product).where(
                    Product.id.in_(product_ids),
                    Product.product_type == "cosmetic",
                    Product.is_active.is_(True),
                )
            )
        ).scalars().all()
    )
    return {product.id: product for product in products}


def _product_dto(
    product: Product, *, owned: bool, equipped: bool, v2: bool = False
) -> dict[str, Any]:
    """DB JSONB를 엄격한 공개 계약으로 검증한 뒤 JSON 직렬화한다."""
    if v2:
        model: type[ShopProduct | ShopProductV2] = ShopProductV2
        slot = product.slot
        assets = rightside_asset_view(product.assets)
    else:
        model = ShopProduct
        slot = _legacy_slot(product.slot)
        assets = legacy_asset_view(product.assets)
    try:
        dto = model(
            id=product.public_id,
            name=product.name,
            slot=slot,
            price_hay=product.price_hay,
            owned=owned,
            equipped=equipped,
            asset_version=product.asset_version,
            assets=assets,
        )
    except ValidationError as exc:
        raise errors.AppError(
            "INTERNAL",
            500,
            "상품 에셋 구성이 올바르지 않습니다.",
            {"product_id": product.public_id},
        ) from exc
    return dto.model_dump(mode="json")


async def get_products(
    session: AsyncSession, user_id: str, *, v2: bool = False
) -> dict[str, Any]:
    uid = _uid(user_id)
    products = list(
        (
            await session.execute(
                select(Product)
                .where(Product.product_type == "cosmetic", Product.is_active.is_(True))
                .order_by(Product.sort_order, Product.public_id)
            )
        ).scalars().all()
    )
    rows = await _user_rows(session, uid)
    owned = {row.product_id for row in rows if row.source != "subscription"}
    equipped = _equipped_product_ids(rows, v2=v2)
    themes: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for product in products:
        dto = _product_dto(
            product, owned=product.id in owned, equipped=product.id in equipped, v2=v2
        )
        (themes if product.slot == "theme" else items).append(dto)
    return {"themes": themes, "items": items}


async def _load_item(session: AsyncSession, public_id: str) -> Product:
    product = (
        await session.execute(
            select(Product).where(
                Product.public_id == public_id,
                Product.product_type == "cosmetic",
                Product.is_active.is_(True),
            )
        )
    ).scalars().first()
    if product is None:
        raise errors.AppError("NOT_FOUND", 404, "상품을 찾을 수 없어요.")
    return product


async def _load_equipment_item(session: AsyncSession, public_id: str) -> Product:
    try:
        return await _load_item(session, public_id)
    except errors.AppError as exc:
        if exc.code == "NOT_FOUND":
            raise errors.validation(
                "존재하지 않는 상품이에요.", {"product_id": public_id}
            ) from exc
        raise


def _purchase_response(
    payload: dict[str, Any], *, user_id: str, idempotency_key: str | None
) -> dict[str, Any]:
    """현재 구매 응답 계약을 저장·재사용 양쪽에서 검증한다.

    비호환 캐시를 새 구매로 재실행하면 차감·지급이 중복될 수 있으므로 반드시
    fail-closed 하고 행도 보존한다 — 삭제는 요청 경로가 아니라 운영 절차
    (scripts/verify_idempotency_responses.py --delete-invalid)에서만 한다(api-inventory.md).
    응답 본문은 로그에 남기지 않는다.
    """
    try:
        return PurchaseResponse.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        _log.error(
            "구매 멱등 응답 스키마 불일치(user=%s key=%s) — "
            "scripts/verify_idempotency_responses.py --delete-invalid로 정리 필요",
            user_id,
            idempotency_key,
        )
        raise errors.AppError(
            "INTERNAL",
            500,
            "일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
        ) from exc


async def purchase(
    session: AsyncSession,
    user_id: str,
    product_id: str,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    uid = _uid(user_id)
    stored_key = (
        f"{SHOP_PURCHASE_KEY_PREFIX}{idempotency_key}" if idempotency_key else None
    )
    if stored_key is not None:
        cached = await session.get(IdempotencyKey, (uid, stored_key))
        if cached is not None:
            return _purchase_response(
                cached.response, user_id=user_id, idempotency_key=stored_key
            )
        await _lock_user(session, uid)
        cached = await session.get(IdempotencyKey, (uid, stored_key))
        if cached is not None:
            return _purchase_response(
                cached.response, user_id=user_id, idempotency_key=stored_key
            )

    product = await _load_item(session, product_id)
    # 기본 지급 비매품도 재구매는 계약상 ALREADY_OWNED가 우선이다.
    if product.id in await _owned_ids(session, uid):
        raise errors.already_owned()
    # None=비매품. 0은 원장 CHECK(amount<>0) 위반으로 500이 되므로 여기서 422로 차단.
    if not product.price_hay:
        raise errors.validation("구매할 수 없는 상품이에요.", {"product_id": product.public_id})
    order = order_service.create_paid_order(
        session, uid, currency="HAY", product=product, unit_price=product.price_hay
    )
    tx = await hay_ledger.apply(
        session, uid, "shop_purchase", -product.price_hay, order_id=order.id
    )
    session.add(
        UserItem(user_id=uid, product_id=product.id, source="purchase", order_id=order.id)
    )
    response = _purchase_response(
        {
            "product_id": product.public_id,
            "order_id": str(order.id),
            "price_hay": product.price_hay,
            "balance_after": tx.balance_after,
        },
        user_id=user_id,
        idempotency_key=stored_key,
    )
    if stored_key is not None:
        session.add(IdempotencyKey(user_id=uid, key=stored_key, response=response))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if stored_key is not None:
            cached = await session.get(IdempotencyKey, (uid, stored_key))
            if cached is not None:
                return _purchase_response(
                    cached.response, user_id=user_id, idempotency_key=stored_key
                )
        raise errors.already_owned() from exc
    return response


async def get_inventory(
    session: AsyncSession, user_id: str, *, v2: bool = False
) -> dict[str, Any]:
    uid = _uid(user_id)
    rows = await _user_rows(session, uid)
    owned_rows = [row for row in rows if row.source != "subscription"]
    products = await _products_by_ids(session, {row.product_id for row in owned_rows})
    equipped = _equipped_product_ids(rows, v2=v2)
    ordered = sorted(products.values(), key=lambda product: (product.sort_order, product.public_id))
    return {
        "data": [
            _product_dto(product, owned=True, equipped=product.id in equipped, v2=v2)
            for product in ordered
        ]
    }


def _equipment_dto(
    rows: list[UserItem], products: dict[uuid.UUID, Product], *, v2: bool = False
) -> dict[str, Any]:
    by_slot: dict[str, str] = {}
    for row in rows:
        if row.equipped_slot is None:
            continue
        product = products.get(row.product_id)
        if product is None or product.public_id is None:
            raise errors.AppError("INTERNAL", 500, "장착 상품이 활성 카탈로그에 없습니다.")
        by_slot[row.equipped_slot] = product.public_id
    if "theme" not in by_slot:
        raise errors.AppError("INTERNAL", 500, "기본 테마 장착 상태가 없습니다.")
    if v2:
        return EquipmentResponseV2(
            theme_id=by_slot["theme"],
            hat_id=by_slot.get("hat"),
            glasses_id=by_slot.get("glasses"),
            neck_id=by_slot.get("neck"),
            body_id=by_slot.get("body"),
        ).model_dump(mode="json")
    return EquipmentResponse(
        theme_id=by_slot["theme"],
        head_id=by_slot.get("hat") or by_slot.get("glasses"),
        neck_id=by_slot.get("neck"),
        body_id=by_slot.get("body"),
    ).model_dump(mode="json")


async def get_equipment(
    session: AsyncSession, user_id: str, *, v2: bool = False
) -> dict[str, Any]:
    uid = _uid(user_id)
    rows = await _user_rows(session, uid)
    equipped_ids = {row.product_id for row in rows if row.equipped_slot is not None}
    return _equipment_dto(rows, await _products_by_ids(session, equipped_ids), v2=v2)


async def _lock_user(session: AsyncSession, uid: uuid.UUID) -> None:
    profile = await session.get(Profile, uid, with_for_update=True)
    if profile is None:
        raise errors.AppError("NOT_FOUND", 404, "프로필을 찾을 수 없어요.")


async def _unequip_row(session: AsyncSession, row: UserItem) -> None:
    if row.source == "subscription":
        await session.delete(row)
    else:
        row.equipped_slot = None
        row.equipped_at = None


async def _resolve_equipment_target(
    session: AsyncSession,
    by_product: dict[uuid.UUID, UserItem],
    public_id: str,
    *,
    accept: set[str],
    slot_label: str,
) -> Product:
    """착용 대상 상품을 로드하고 슬롯 일치·소유를 검증한다."""
    product = await _load_equipment_item(session, public_id)
    if product.slot not in accept:
        raise errors.validation("슬롯이 맞지 않아요.", {"slot": slot_label})
    row = by_product.get(product.id)
    if row is None or row.source == "subscription":
        raise errors.not_owned()
    return product


async def _apply_targets(
    session: AsyncSession,
    by_product: dict[uuid.UUID, UserItem],
    by_slot: dict[str, UserItem],
    targets: dict[str, Product | None],
    now: datetime,
) -> None:
    """슬롯별 대상(None=해제)으로 장착 상태를 교체한다. 기존 해제를 먼저 flush해
    슬롯 unique 인덱스 충돌을 피한다."""
    to_equip: list[tuple[str, Product]] = []
    for slot, product in targets.items():
        current = by_slot.get(slot)
        if product is not None and current is not None and current.product_id == product.id:
            continue
        if current is not None:
            await _unequip_row(session, current)
        if product is not None:
            to_equip.append((slot, product))
    await session.flush()
    for slot, product in to_equip:
        row = by_product[product.id]
        row.equipped_slot = slot
        row.equipped_at = now
    await session.commit()


async def put_equipment(session: AsyncSession, user_id: str, req) -> dict[str, Any]:
    """레거시(구버전) 장착 — head_id는 실제 hat|glasses 슬롯으로 해석하고 나머지 head 슬롯은 해제한다."""
    uid = _uid(user_id)
    await _lock_user(session, uid)  # 사용자별 PUT 직렬화
    rows = await _user_rows(session, uid)
    by_product = {row.product_id: row for row in rows}
    by_slot = {row.equipped_slot: row for row in rows if row.equipped_slot is not None}
    now = datetime.now(timezone.utc)

    targets: dict[str, Product | None] = {"hat": None, "glasses": None}
    for slot in ("theme", "neck", "body"):
        public_id = getattr(req, f"{slot}_id")
        targets[slot] = (
            await _resolve_equipment_target(
                session, by_product, public_id, accept={slot}, slot_label=slot
            )
            if public_id is not None
            else None
        )
    if req.head_id is not None:
        product = await _resolve_equipment_target(
            session, by_product, req.head_id, accept={"hat", "glasses"}, slot_label="head"
        )
        targets[product.slot] = product

    await _apply_targets(session, by_product, by_slot, targets, now)

    return EquipmentResponse(
        theme_id=req.theme_id,
        head_id=req.head_id,
        neck_id=req.neck_id,
        body_id=req.body_id,
    ).model_dump(mode="json")


async def put_equipment_v2(session: AsyncSession, user_id: str, req) -> dict[str, Any]:
    """v2 장착 — hat/glasses를 독립 슬롯으로 동시 착용한다."""
    uid = _uid(user_id)
    await _lock_user(session, uid)  # 사용자별 PUT 직렬화
    rows = await _user_rows(session, uid)
    by_product = {row.product_id: row for row in rows}
    by_slot = {row.equipped_slot: row for row in rows if row.equipped_slot is not None}
    now = datetime.now(timezone.utc)

    targets: dict[str, Product | None] = {}
    for slot in _SLOTS_V2:
        public_id = getattr(req, f"{slot}_id")
        targets[slot] = (
            await _resolve_equipment_target(
                session, by_product, public_id, accept={slot}, slot_label=slot
            )
            if public_id is not None
            else None
        )

    await _apply_targets(session, by_product, by_slot, targets, now)

    return EquipmentResponseV2(
        theme_id=req.theme_id,
        hat_id=req.hat_id,
        glasses_id=req.glasses_id,
        neck_id=req.neck_id,
        body_id=req.body_id,
    ).model_dump(mode="json")
