"""Only today's selected mood is available to chat; journal text is never read."""
from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mood import MoodEntry

# Matches the app's MoodKind. The storage API still accepts future/custom kinds.
_MOOD_KINDS = ("annoyed", "tired", "neutral", "content", "excited")
_log = logging.getLogger("moly-backend")


async def today_block(session: AsyncSession, user_id: uuid.UUID, today: date) -> str:
    """Read one allowed value for the local calendar day, without fetching free text."""
    mood = "unavailable"
    try:
        async with session.begin_nested():
            mood = (await session.execute(
                select(case((MoodEntry.kind.in_(_MOOD_KINDS), MoodEntry.kind), else_="unavailable"))
                .where(MoodEntry.user_id == user_id, MoodEntry.entry_date == today)
            )).scalar_one_or_none() or "not_recorded"
    except Exception:  # noqa: BLE001 — optional context must not prevent a reply
        mood = "unavailable"
        _log.warning("Today's mood read unavailable user=%s", user_id)
    return f"[Today's mood] {today.isoformat()}: {mood}"
