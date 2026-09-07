"""날짜별 감정 기록. 클라이언트의 현지 날짜를 시간대 변환 없이 보존한다."""
from __future__ import annotations

import calendar
from datetime import date
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.mood import MoodEntry
from app.schemas.mood import MoodPutRequest
from app.services import privacy
from app.services.account import _uid


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise errors.validation("올바른 날짜를 입력해 주세요.") from exc


def _item(entry: MoodEntry) -> dict[str, Any]:
    return {"date": entry.entry_date, "kind": entry.kind, "note": entry.note}


async def list_moods(session: AsyncSession, user_id: str, month: str) -> dict[str, Any]:
    uid = _uid(user_id)
    await privacy.ensure_subject_active(session, uid)
    first = _date(f"{month}-01")
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    rows = (await session.execute(
        select(MoodEntry).where(
            MoodEntry.user_id == uid,
            MoodEntry.entry_date >= first,
            MoodEntry.entry_date <= last,
        ).order_by(MoodEntry.entry_date.desc())
    )).scalars().all()
    return {"data": [_item(row) for row in rows]}


async def put_mood(
    session: AsyncSession, user_id: str, entry_date: str, req: MoodPutRequest
) -> dict[str, Any]:
    uid = _uid(user_id)
    await privacy.ensure_subject_active(session, uid)
    day = _date(entry_date)
    stmt = pg_insert(MoodEntry).values(
        user_id=uid, entry_date=day, kind=req.kind, note=req.note
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[MoodEntry.user_id, MoodEntry.entry_date],
        set_={"kind": stmt.excluded.kind, "note": stmt.excluded.note, "updated_at": func.now()},
    ).returning(MoodEntry)
    entry = (await session.execute(stmt)).scalar_one()
    result = _item(entry)
    await session.commit()
    return result


async def delete_mood(session: AsyncSession, user_id: str, entry_date: str) -> None:
    uid = _uid(user_id)
    await privacy.ensure_subject_active(session, uid)
    await session.execute(delete(MoodEntry).where(
        MoodEntry.user_id == uid, MoodEntry.entry_date == _date(entry_date)
    ))
    await session.commit()
