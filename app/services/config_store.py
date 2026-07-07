"""app_config 서버 설정 접근 — 서버 판정용 값(한도·임계) 조회. 클라 노출과 분리."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_config import AppConfig


async def get_config_values(session: AsyncSession, keys: list[str]) -> dict[str, Any]:
    """여러 key의 value(jsonb)를 dict로. 없는 key는 결과에서 빠짐(호출측이 기본값 처리)."""
    rows = await session.execute(select(AppConfig).where(AppConfig.key.in_(keys)))
    return {row.key: row.value for row in rows.scalars()}
