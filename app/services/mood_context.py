"""Bounded, current reads of user-written mood entries for conversation input."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mood import MoodEntry
from app.services import i18n
from app.services.memory import sanitize_text

MAX_KIND_CHARS = 40
MAX_NOTE_CHARS = 400
_log = logging.getLogger("moly-backend")

# An entry is not a disclosure in chat. Keep that distinction adjacent to the data.
_CHAT_BOUNDARY = {
    "ko": (
        "이 일기는 대화에서 들은 말이 아니야. 일기를 읽거나 활용해 달라는 직접 요청이 없으면 "
        "이 일기에서는 상대가 대화에서 직접 말한 동일한 감정·사실만 참고해. 비슷한 화제라도 일기에만 있는 "
        "감정·세부사항으로 지금 기분을 추측하거나 이야기 소재를 정하지 마."
    ),
    "en": (
        "This journal was not spoken in chat. Without an explicit request to read/use it, "
        "this journal may only support the same details they stated in their own chat messages. A related topic "
        "does not permit journal-only feelings/details to determine their current mood or story material."
    ),
    "ja": (
        "この日記は会話で聞いた言葉ではない。日記を読んだり使ったりしてほしいという明確な "
        "依頼がなければ、この日記から参考にできるのは相手が会話で自分から話した同じ気分や事実だけ。関連する話題でも、 "
        "日記だけにある気分や詳細で今の気分を推測したり、話の題材を決めたりしない。"
    ),
}


def clipped(value: str, cap: int) -> tuple[str, bool]:
    value = sanitize_text(value)
    return (value[:cap - 1] + "…", True) if len(value) > cap else (value, False)


async def read_entries(
    session: AsyncSession, user_id: uuid.UUID, start: date, end: date,
) -> list[dict]:
    """At most 31 calendar days; bound free text before transferring it from the DB."""
    if not 0 <= (end - start).days < 31:
        raise ValueError("mood_date_range")
    rows = (await session.execute(
        select(
            MoodEntry.entry_date,
            func.left(MoodEntry.kind, MAX_KIND_CHARS + 1).label("kind"),
            func.left(MoodEntry.note, MAX_NOTE_CHARS + 1).label("note"),
            ((func.length(MoodEntry.kind) > MAX_KIND_CHARS)
             | (func.length(MoodEntry.note) > MAX_NOTE_CHARS)).label("truncated"),
            MoodEntry.created_at,
            MoodEntry.updated_at,
        ).where(
            MoodEntry.user_id == user_id,
            MoodEntry.entry_date >= start,
            MoodEntry.entry_date <= end,
        ).order_by(MoodEntry.entry_date.desc()).limit(31)
    )).mappings().all()
    entries = []
    for row in rows:
        kind, kind_cut = clipped(row["kind"], MAX_KIND_CHARS)
        note, note_cut = clipped(row["note"], MAX_NOTE_CHARS)
        entries.append({
            "date": row["entry_date"].isoformat(), "kind": kind, "note": note,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "truncated": kind_cut or note_cut or row["truncated"],
        })
    return entries


async def today_block(
    session: AsyncSession, user_id: uuid.UUID, today: date, *, zone: ZoneInfo, language: str = "ko",
) -> str:
    """Fresh each turn. Failure is isolated from the caller's transaction by a savepoint."""
    data: dict = {"date": today.isoformat(), "status": "absent"}
    try:
        async with session.begin_nested():
            entries = await read_entries(session, user_id, today, today)
        if entries:
            entry = entries[0]
            data = {"status": "present", **entry}
            for key in ("created_at", "updated_at"):
                value: datetime | None = data[key]
                data[key] = value.astimezone(zone).isoformat(timespec="minutes") if value else None
            if data["updated_at"] == data["created_at"]:
                del data["updated_at"]
            if not data["truncated"]:
                del data["truncated"]
    except Exception:  # noqa: BLE001 — optional context must not prevent a reply
        _log.warning("Today's mood read unavailable user=%s", user_id)
        data = {"date": today.isoformat(), "status": "unavailable"}
    block = "[User mood entry]\n" + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if data["status"] == "present":
        block += "\n" + _CHAT_BOUNDARY[i18n.resolve(language)]
    return block
