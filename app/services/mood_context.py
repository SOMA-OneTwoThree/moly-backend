"""Only today's selected mood is available to chat; journal text is never read."""
from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mood import MoodEntry
from app.services import i18n

# Same labels as the app, so the model does not reinterpret the storage codes.
# The storage API still accepts future/custom kinds; chat only uses these five.
_MOOD_LABELS = {
    "annoyed": {"ko": "짜증", "en": "Annoyed", "ja": "イライラ"},
    "tired": {"ko": "피곤", "en": "Tired", "ja": "つかれた"},
    "neutral": {"ko": "평범", "en": "Okay", "ja": "ふつう"},
    "content": {"ko": "편안", "en": "Content", "ja": "おだやか"},
    "excited": {"ko": "신남", "en": "Excited", "ja": "ごきげん"},
}
_log = logging.getLogger("moly-backend")


async def today_block(
    session: AsyncSession, user_id: uuid.UUID, today: date, *, language: str = "ko",
) -> str:
    """Read one allowed value for the local calendar day, without fetching free text."""
    mood = "unknown"
    try:
        async with session.begin_nested():
            mood = (await session.execute(
                select(case((MoodEntry.kind.in_(tuple(_MOOD_LABELS)), MoodEntry.kind), else_="unknown"))
                .where(MoodEntry.user_id == user_id, MoodEntry.entry_date == today)
            )).scalar_one_or_none() or "unknown"
    except Exception:  # noqa: BLE001 — optional context must not prevent a reply
        mood = "unknown"
        _log.warning("Today's mood read unavailable user=%s", user_id)
    labels = _MOOD_LABELS.get(mood)
    label = labels[i18n.resolve(language)] if labels else "unknown"
    return f"[User mood selection] {today.isoformat()}: {label}"
