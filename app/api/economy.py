"""건초·충전소 API. 전 엔드포인트 Bearer 인증."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.economy import (
    ChargingStationResponse,
    RewardResponse,
    TransactionsResponse,
    WalletResponse,
)
from app.services import economy

router = APIRouter(tags=["economy"])


@router.get("/wallet", response_model=WalletResponse)
async def wallet(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await economy.get_wallet(session, user_id)


@router.get("/wallet/transactions", response_model=TransactionsResponse)
async def transactions(
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await economy.list_transactions(session, user_id, limit=limit, cursor=cursor)


@router.get("/charging-station", response_model=ChargingStationResponse)
async def charging_station(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await economy.get_charging_status(session, user_id)


@router.post("/charging-station/attendance", response_model=RewardResponse)
async def attendance(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await economy.claim_attendance(session, user_id)


@router.post("/charging-station/routine-reward", response_model=RewardResponse)
async def routine_reward(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await economy.claim_routine_reward(session, user_id)
