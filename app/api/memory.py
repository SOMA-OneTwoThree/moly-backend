"""정규화 기억 조회·검색·명시적 망각 API. 전 엔드포인트 Bearer 인증."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.memory import (
    MemoryForgetRequest,
    MemoryForgetResponse,
    MemoryListResponse,
    MemorySearchRequest,
    MemorySearchResponse,
)
from app.services import memory_api

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("", response_model=MemoryListResponse)
async def list_memory(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await memory_api.list_facts(session, user_id)


@router.post("/search", response_model=MemorySearchResponse)
async def search_memory(
    req: MemorySearchRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await memory_api.search(session, user_id, req)


@router.post("/forget", response_model=MemoryForgetResponse)
async def forget_memory(
    req: MemoryForgetRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await memory_api.forget(session, user_id, req)
