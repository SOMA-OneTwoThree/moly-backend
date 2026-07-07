"""app-config 서비스 — 클라 노출 키만 선별(API_SPEC §2).

서버 판정용 임계(diary_llm_min_tokens·review_prompt_min_tokens 등)는 노출 안 함.
클라가 필요한 부팅 설정만: min_supported_version · maintenance · day_night_schedule.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models.app_config import AppConfig

# 클라에 내려줄 키(화이트리스트).
_CLIENT_KEYS = ("min_supported_version", "maintenance", "day_night_schedule")


async def get_public_app_config(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = await session.execute(
        select(AppConfig).where(AppConfig.key.in_(_CLIENT_KEYS))
    )
    data = {row.key: row.value for row in rows.scalars()}
    return {
        "min_supported_version": data.get("min_supported_version", "1.0.0"),
        "maintenance": data.get("maintenance", {"active": False, "message": None}),
        "day_night_schedule": data.get("day_night_schedule"),
    }
