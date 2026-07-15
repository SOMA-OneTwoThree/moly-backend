"""diary 서비스 — 조회·상세·열람표시. 생성은 워커(04:00 배치). 열람은 등급무관 무료.

노출 규칙: published_at ≤ now 인 건만(배치 생성분의 발행 전 노출 방지, API_SPEC §4).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.diary import Diary
from app.models.profile import Profile
from app.services import greetings
from app.services.account import _uid

_PREVIEW_LEN = 60

# 웰컴 일기 — 온보딩 후 첫 일기. 제목은 본문 첫 줄(제목 컬럼이 없어 스키마 무변경).
# 닉네임만 교체. source=preset(→ type=moly, 대화링크 없음). 자세한 배치는 ensure_welcome 참조.
_WELCOME_BODY = (
    "오늘은 뒹굴거리다가 새 친구를 만났다. 이름은 {name}.\n"
    "말하는 카피바라라니 신기하다고 했다. 나는 그 말이 조금 웃겼다. 나한테는 그 친구가 더 신기한데.\n"
    "우리 집도 보여줬다. 낮잠 자는 자리랑, 음악 듣는 자리랑. 어떤 친구일까? 또 대화해보고 싶다."
)


def _welcome_content(nickname: str) -> str:
    return f"{greetings.with_wa(nickname)}의 만남\n\n{_WELCOME_BODY.format(name=nickname)}"


def _welcome_date(created_at: datetime, tz: str) -> date:
    """웰컴 일기 날짜 = 가입일(로컬)-1. 가입일은 비워 워커의 첫날 개인일기와 안 겹치게 한다."""
    return created_at.astimezone(ZoneInfo(tz)).date() - timedelta(days=1)


async def ensure_welcome(session: AsyncSession, user_id: str) -> None:
    """웰컴 일기 1회 삽입 — 가입일-1(가장 오래된 일기)에 고정.

    가입일 슬롯은 비워 둔다. 그래야 다음날 워커가 '가입일' 개인일기를 정상 생성한다(웰컴과 별개).
    diary_date가 고정이라 UNIQUE(user, date)+ON CONFLICT로 멱등 — 몇 번 조회해도 한 번만 생긴다.
    닉네임/가입시각이 없으면(온보딩 전) 건너뛰고, 온보딩 후 다음 조회에서 만든다.
    """
    uid = _uid(user_id)
    profile = await session.get(Profile, uid)
    if profile is None or not profile.nickname or profile.created_at is None:
        return
    stmt = (
        pg_insert(Diary)
        .values(
            user_id=uid,
            diary_date=_welcome_date(profile.created_at, profile.timezone),
            source="preset",
            preset_ment_id=None,
            content=_welcome_content(profile.nickname),
            weather="sunny",
            published_at=datetime.now(timezone.utc),  # 즉시 노출
        )
        .on_conflict_do_nothing(index_elements=["user_id", "diary_date"])
    )
    await session.execute(stmt)
    await session.commit()


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
    await ensure_welcome(session, user_id)  # 첫 조회 때 웰컴 일기 lazy 생성(멱등)
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
