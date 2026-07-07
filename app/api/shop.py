"""상점·꾸미기 API. 전 엔드포인트 Bearer 인증."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.shop import EquipmentPutRequest, PurchaseRequest
from app.services import shop

router = APIRouter(tags=["shop"])


@router.get("/shop/products")
async def products(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.get_products(session, user_id)


@router.post("/shop/purchases")
async def purchase(
    req: PurchaseRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.purchase(session, user_id, req.product_id)


@router.get("/inventory")
async def inventory(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.get_inventory(session, user_id)


@router.get("/inventory/equipment")
async def get_equipment(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.get_equipment(session, user_id)


@router.put("/inventory/equipment")
async def put_equipment(
    req: EquipmentPutRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await shop.put_equipment(session, user_id, req)
