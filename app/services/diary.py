"""diary 서비스 — 조회·상세·열람표시. 생성은 워커(04:00 배치). 열람은 등급무관 무료.

노출 규칙: published_at ≤ now 인 건만(배치 생성분의 발행 전 노출 방지, API_SPEC §4).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.diary import Diary
from app.services.account import _uid

_PREVIEW_LEN = 60


def _type(source: str) -> str:
    return "personal" if source == "llm" else "moly"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _list_item(d: Diary) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "diary_date": d.diary_date.isoformat(),
        "type": _type(d.source),
        "weather": d.weather,
        "preview": (d.content or "")[:_PREVIEW_LEN],
        "published_at": _iso(d.published_at),
        "read": d.first_read_at is not None,
    }


async def list_diaries(
    session: AsyncSession, user_id: str, *, limit: int = 30, cursor: str | None = None
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    limit = max(1, min(limit, 100))
    q = select(Diary).where(Diary.user_id == _uid(user_id), Diary.published_at <= now)
    if cursor:
        try:
            cursor_date = date.fromisoformat(cursor)
        except ValueError as e:
            raise errors.validation("잘못된 커서 형식이에요.") from e
        q = q.where(Diary.diary_date < cursor_date)
    q = q.order_by(Diary.diary_date.desc()).limit(limit + 1)  # +1로 다음 페이지 유무 판별
    rows = list((await session.execute(q)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].diary_date.isoformat() if (has_more and rows) else None
    return {"data": [_list_item(d) for d in rows], "next_cursor": next_cursor}


async def _load_published(session: AsyncSession, user_id: str, diary_id: str) -> Diary:
    try:
        did = uuid.UUID(diary_id)
    except ValueError as e:
        raise errors.AppError("NOT_FOUND", 404, "일기를 찾을 수 없어요.") from e
    d = await session.get(Diary, did)
    now = datetime.now(timezone.utc)
    if (
        d is None
        or d.user_id != _uid(user_id)
        or d.published_at is None
        or d.published_at > now
    ):
        raise errors.AppError("NOT_FOUND", 404, "일기를 찾을 수 없어요.")
    return d


async def get_diary(session: AsyncSession, user_id: str, diary_id: str) -> dict[str, Any]:
    d = await _load_published(session, user_id, diary_id)
    is_personal = _type(d.source) == "personal"
    return {
        "id": str(d.id),
        "diary_date": d.diary_date.isoformat(),
        "type": _type(d.source),
        "weather": d.weather,
        "body": d.content,
        "conversation_ref": {"anchor_date": d.diary_date.isoformat()} if is_personal else None,
        "published_at": _iso(d.published_at),
        "first_read_at": _iso(d.first_read_at),
    }


async def mark_read(session: AsyncSession, user_id: str, diary_id: str) -> None:
    d = await _load_published(session, user_id, diary_id)
    if d.first_read_at is None:  # 멱등 — 최초만 기록
        d.first_read_at = datetime.now(timezone.utc)
        await session.commit()
