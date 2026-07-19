"""상점·꾸미기 API. 전 엔드포인트 Bearer 인증."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.shop import (
    EquipmentPutRequest,
    EquipmentPutRequestV2,
    EquipmentResponse,
    EquipmentResponseV2,
    InventoryResponse,
    InventoryResponseV2,
    ProductsResponse,
    ProductsResponseV2,
    PurchaseRequest,
    PurchaseResponse,
)
from app.services import shop

router = APIRouter(tags=["shop"])


@router.get("/shop/products", response_model=ProductsResponse)
async def products(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.get_products(session, user_id)


@router.post("/shop/purchases", response_model=PurchaseResponse)
async def purchase(
    req: PurchaseRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1
    ),
) -> dict[str, Any]:
    return await shop.purchase(
        session, user_id, req.product_id, idempotency_key=idempotency_key
    )


@router.get("/inventory", response_model=InventoryResponse)
async def inventory(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.get_inventory(session, user_id)


@router.get("/inventory/equipment", response_model=EquipmentResponse)
async def get_equipment(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.get_equipment(session, user_id)


@router.put("/inventory/equipment", response_model=EquipmentResponse)
async def put_equipment(
    req: EquipmentPutRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.put_equipment(session, user_id, req)


# ── v2: head 슬롯을 hat/glasses로 분리한 신버전 계약. 구버전 앱은 위 레거시 경로를 계속 쓴다.
@router.get("/v2/shop/products", response_model=ProductsResponseV2)
async def products_v2(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.get_products(session, user_id, v2=True)


@router.get("/v2/inventory", response_model=InventoryResponseV2)
async def inventory_v2(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.get_inventory(session, user_id, v2=True)


@router.get("/v2/inventory/equipment", response_model=EquipmentResponseV2)
async def get_equipment_v2(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.get_equipment(session, user_id, v2=True)


@router.put("/v2/inventory/equipment", response_model=EquipmentResponseV2)
async def put_equipment_v2(
    req: EquipmentPutRequestV2,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.put_equipment_v2(session, user_id, req)
