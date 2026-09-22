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
from app.core.pg import unique_violation
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
from app.services import hay_ledger, i18n
from app.services import order as order_service
from app.services.account import _load_active_subscription, _load_profile, _uid

_SLOTS_V2 = ("theme", "hat", "glasses", "neck", "body")

_log = logging.getLogger("moly-backend")


def supports_timer_clothing(capabilities: str | None) -> bool:
    return "timer-clothing-v1" in {token.strip() for token in (capabilities or "").split(",")}


def supports_subscriber_only(capabilities: str | None) -> bool:
    return "subscriber-only-v1" in {token.strip() for token in (capabilities or "").split(",")}


DEFAULT_THEME_PUBLIC_ID = "theme_default"


def bundled_theme_ids(header: str | None) -> frozenset[str]:
    return frozenset(token.strip() for token in (header or "").split(",") if token.strip())


def _hidden_product(
    product: Product, *, v2: bool, timer_capable: bool,
    bundled_themes: frozenset[str] = frozenset(),
) -> bool:
    return (
        (not v2 and product.is_v2_only)
        or (not timer_capable and "timer" in (product.assets.get("rightside") or {}))
        or (bool(product.assets.get("bundled")) and product.public_id not in bundled_themes)
    )


def legacy_asset_view(assets: dict[str, Any]) -> dict[str, Any]:
    """레거시(구버전) 응답 — 새 자세 키를 숨겨 기존 계약 형태를 그대로 유지한다."""
    return {key: value for key, value in assets.items() if key not in ("rightside", "bundled")}


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
        **({"timer": rightside["timer"]} if "timer" in rightside else {}),
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


async def _subscribed(session: AsyncSession, user_id: str) -> bool:
    """구독 전용 상품의 사용권. 체험·런칭 무료 기간은 포함하지 않는다."""
    return await _load_active_subscription(session, user_id, datetime.now(timezone.utc)) is not None


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
    product: Product, *, owned: bool, equipped: bool, v2: bool = False, language: str | None = None
) -> dict[str, Any]:
    """DB JSONB를 엄격한 공개 계약으로 검증한 뒤 JSON 직렬화한다. 이름은 유저 언어로(SOMA-346)."""
    if v2:
        model: type[ShopProduct | ShopProductV2] = ShopProductV2
        slot = product.slot
        assets = rightside_asset_view(product.assets)
        v2_fields = {"subscriber_only": product.is_subscriber_only}
    else:
        model = ShopProduct
        slot = _legacy_slot(product.slot)
        assets = legacy_asset_view(product.assets)
        v2_fields = {}
    try:
        dto = model(
            id=product.public_id,
            name=i18n.localized_name(
                product.name_i18n, language, product.name, kind="product", key=product.public_id or ""
            ),
            slot=slot,
            price_hay=product.price_hay,
            owned=owned,
            equipped=equipped,
            asset_version=product.asset_version,
            assets=assets,
            **v2_fields,
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
    session: AsyncSession, user_id: str, *, v2: bool = False, timer_capable: bool = False,
    bundled_themes: frozenset[str] = frozenset(), subscriber_capable: bool = False,
) -> dict[str, Any]:
    profile = await _load_profile(session, user_id)
    uid = profile.id
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
    # 구독이 끝났거나 구독 전용을 모르는 앱이면 그 장착은 풀린 것으로 보인다. 테마 자리는 기본 테마가 채운다.
    locked = [product for product in products if product.is_subscriber_only and product.id in equipped]
    if locked and not (subscriber_capable and await _subscribed(session, user_id)):
        equipped -= {product.id for product in locked}
        if any(product.slot == "theme" for product in locked):
            equipped |= {product.id for product in products if product.public_id == DEFAULT_THEME_PUBLIC_ID}
    themes: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for product in products:
        if _hidden_product(product, v2=v2, timer_capable=timer_capable, bundled_themes=bundled_themes):
            continue
        if product.is_subscriber_only and not subscriber_capable:
            continue
        dto = _product_dto(
            product, owned=product.id in owned, equipped=product.id in equipped, v2=v2,
            language=profile.language,
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
    timer_capable: bool = False,
    bundled_themes: frozenset[str] = frozenset(),
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
    if _hidden_product(product, v2=True, timer_capable=timer_capable, bundled_themes=bundled_themes):
        raise errors.AppError("NOT_FOUND", 404, "상품을 찾을 수 없어요.")
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
        # 예상 밖 IntegrityError(NULL/FK 등)는 already_owned로 위장하지 않고 전파(500, 은폐 금지).
        if not unique_violation(exc, "user_items_user_product_uq", "idempotency_keys_pkey"):
            raise
        if stored_key is not None:
            cached = await session.get(IdempotencyKey, (uid, stored_key))
            if cached is not None:
                return _purchase_response(
                    cached.response, user_id=user_id, idempotency_key=stored_key
                )
        raise errors.already_owned() from exc
    return response


async def get_inventory(
    session: AsyncSession, user_id: str, *, v2: bool = False, timer_capable: bool = False,
    bundled_themes: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    profile = await _load_profile(session, user_id)
    uid = profile.id
    rows = await _user_rows(session, uid)
    owned_rows = [row for row in rows if row.source != "subscription"]
    products = await _products_by_ids(session, {row.product_id for row in owned_rows})
    equipped = _equipped_product_ids(rows, v2=v2)
    ordered = sorted(products.values(), key=lambda product: (product.sort_order, product.public_id))
    return {
        "data": [
            _product_dto(product, owned=True, equipped=product.id in equipped, v2=v2,
                         language=profile.language)
            for product in ordered
            if not _hidden_product(product, v2=v2, timer_capable=timer_capable, bundled_themes=bundled_themes)
        ]
    }


def _equipment_dto(
    rows: list[UserItem], products: dict[uuid.UUID, Product], *, v2: bool = False,
    timer_capable: bool = False,
    bundled_themes: frozenset[str] = frozenset(),
    subscriber_only_usable: bool = True,
) -> dict[str, Any]:
    by_slot: dict[str, str] = {}
    for row in rows:
        if row.equipped_slot is None:
            continue
        product = products.get(row.product_id)
        if product is None or product.public_id is None:
            raise errors.AppError("INTERNAL", 500, "장착 상품이 활성 카탈로그에 없습니다.")
        if product.is_subscriber_only and not subscriber_only_usable:
            if row.equipped_slot == "theme":
                by_slot["theme"] = DEFAULT_THEME_PUBLIC_ID
            continue
        if row.equipped_slot == "body" and _hidden_product(
            product, v2=v2, timer_capable=timer_capable, bundled_themes=bundled_themes
        ):
            continue
        if row.equipped_slot == "theme" and _hidden_product(
            product, v2=v2, timer_capable=timer_capable, bundled_themes=bundled_themes
        ):
            by_slot["theme"] = DEFAULT_THEME_PUBLIC_ID
            continue
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
    session: AsyncSession, user_id: str, *, v2: bool = False, timer_capable: bool = False,
    bundled_themes: frozenset[str] = frozenset(), subscriber_capable: bool = False,
) -> dict[str, Any]:
    uid = _uid(user_id)
    rows = await _user_rows(session, uid)
    equipped_ids = {row.product_id for row in rows if row.equipped_slot is not None}
    products = await _products_by_ids(session, equipped_ids)
    locked = any(product.is_subscriber_only for product in products.values())
    return _equipment_dto(
        rows, products, v2=v2, timer_capable=timer_capable,
        bundled_themes=bundled_themes,
        subscriber_only_usable=not locked or (
            subscriber_capable and await _subscribed(session, user_id)
        ),
    )


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
    user_id: str,
    by_product: dict[uuid.UUID, UserItem],
    public_id: str,
    *,
    accept: set[str],
    slot_label: str,
) -> Product:
    """착용 대상 상품을 로드하고 슬롯 일치와 소유(구독 전용이면 활성 구독)를 검증한다."""
    product = await _load_equipment_item(session, public_id)
    if product.slot not in accept:
        raise errors.validation("슬롯이 맞지 않아요.", {"slot": slot_label})
    if product.is_subscriber_only:
        if not await _subscribed(session, user_id):
            raise errors.not_owned()
        return product
    row = by_product.get(product.id)
    if row is None or row.source == "subscription":
        raise errors.not_owned()
    return product


async def _apply_targets(
    session: AsyncSession,
    uid: uuid.UUID,
    by_product: dict[uuid.UUID, UserItem],
    by_slot: dict[str, UserItem],
    targets: dict[str, Product | None],
    now: datetime,
) -> None:
    """슬롯별 대상(None=해제)으로 장착 상태를 교체한다. 기존 해제를 먼저 flush해
    슬롯 unique 인덱스 충돌을 피한다. 구독 전용 상품은 장착하는 동안만 subscription 행을 둔다."""
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
        row = by_product.get(product.id)
        if row is None:
            row = UserItem(user_id=uid, product_id=product.id, source="subscription")
            session.add(row)
        row.equipped_slot = slot
        row.equipped_at = now
    await session.commit()


async def _compatible_body(
    session: AsyncSession,
    by_slot: dict[str, UserItem],
    targets: dict[str, Product | None],
    *,
    v2: bool = False,
    timer_capable: bool = False,
    bundled_themes: frozenset[str] = frozenset(),
) -> str | None:
    target = targets["body"]
    if target is not None:
        hidden = _hidden_product(
            target, v2=v2, timer_capable=timer_capable, bundled_themes=bundled_themes
        )
        return None if hidden else target.public_id
    current = by_slot.get("body")
    if current is not None and not (v2 and timer_capable):
        products = await _products_by_ids(session, {current.product_id})
        product = products.get(current.product_id)
        if product is not None and _hidden_product(
            product, v2=v2, timer_capable=timer_capable, bundled_themes=bundled_themes
        ):
            # 구 앱은 숨겨진 몸을 null로 돌려보낸다. 다른 슬롯만 갱신한다.
            del targets["body"]
    return None


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
                session, user_id, by_product, public_id, accept={slot}, slot_label=slot
            )
            if public_id is not None
            else None
        )
    if req.head_id is not None:
        product = await _resolve_equipment_target(
            session, user_id, by_product, req.head_id, accept={"hat", "glasses"}, slot_label="head"
        )
        targets[product.slot] = product

    body_id = await _compatible_body(session, by_slot, targets)
    await _apply_targets(session, uid, by_product, by_slot, targets, now)

    return EquipmentResponse(
        theme_id=req.theme_id,
        head_id=req.head_id,
        neck_id=req.neck_id,
        body_id=body_id,
    ).model_dump(mode="json")


async def put_equipment_v2(
    session: AsyncSession, user_id: str, req, *, timer_capable: bool = False,
    bundled_themes: frozenset[str] = frozenset(),
) -> dict[str, Any]:
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
                session, user_id, by_product, public_id, accept={slot}, slot_label=slot
            )
            if public_id is not None
            else None
        )

    body_id = await _compatible_body(
        session, by_slot, targets, v2=True, timer_capable=timer_capable,
        bundled_themes=bundled_themes,
    )
    await _apply_targets(session, uid, by_product, by_slot, targets, now)

    return EquipmentResponseV2(
        theme_id=req.theme_id,
        hat_id=req.hat_id,
        glasses_id=req.glasses_id,
        neck_id=req.neck_id,
        body_id=body_id,
    ).model_dump(mode="json")
