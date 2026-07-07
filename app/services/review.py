"""리뷰 — 노출 기록(계정당 1회). 노출 판정은 chat 응답 review_prompt 플래그(ERD §9)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.account import _load_profile


async def mark_prompted(session: AsyncSession, user_id: str) -> None:
    profile = await _load_profile(session, user_id)
    if profile.review_prompted_at is None:  # 최초 1회만
        profile.review_prompted_at = datetime.now(timezone.utc)
        await session.commit()
