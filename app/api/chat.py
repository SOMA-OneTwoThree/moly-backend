"""대화 API — 상태·이력·전송·선발화. 전 엔드포인트 Bearer 인증."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.chat import (
    ChatStateResponse,
    GreetingResponse,
    MessagesResponse,
    PostMessageRequest,
    PostMessageResponse,
)
from app.services import chat as chat_service

router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("/state", response_model=ChatStateResponse)
async def get_state(
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await chat_service.get_state(session, user_id)


@router.get("/messages", response_model=MessagesResponse)
async def get_messages(
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
    direction: str = Query("older", pattern="^(older|newer)$"),
    anchor_date: date | None = Query(None),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await chat_service.get_messages(
        session, user_id, limit=limit, cursor=cursor, direction=direction, anchor_date=anchor_date
    )


@router.post("/messages", response_model=PostMessageResponse)
async def post_message(
    req: PostMessageRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PostMessageResponse:
    if not idempotency_key:
        raise errors.validation("Idempotency-Key 헤더가 필요해요.")
    return await chat_service.post_message(session, user_id, req, idempotency_key)


@router.get("/greeting", response_model=GreetingResponse)
async def get_greeting(
    context: str = Query(...),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await chat_service.get_greeting(session, user_id, context)
