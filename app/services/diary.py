"""diary 서비스 — 조회·상세·열람표시. 생성은 워커(04:00 배치). 열람은 등급무관 무료.

노출 규칙: published_at ≤ now 인 건만(배치 생성분의 발행 전 노출 방지, API_SPEC §4).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.time_utils import activity_date_for
from app.models.diary import Diary
from app.models.profile import Profile
from app.services import naming
from app.services.account import _uid

_PREVIEW_LEN = 60

# 웰컴 일기 — 온보딩 후 첫 일기. content = '제목\n\n본문'(제목 컬럼 없어 스키마 무변경).
# 이름은 placeholder 토큰으로만 저장하고 egress에서 현재 닉네임으로 렌더한다(개명 드리프트 방지).
# source=welcome(→ type=moly, title 필드로 분리 노출). 자세한 배치는 ensure_welcome 참조.
_WELCOME_CONTENT = (
    "{유저이름}, 첫 만남\n\n"
    "오늘은 뒹굴거리다가 새 친구를 만났다. 이름은 {유저이름}.\n"
    "말하는 카피바라라니 신기하다고 했다. 나는 그 말이 조금 웃겼다. 나한테는 그 친구가 더 신기한데.\n"
    "우리 집도 보여줬다. 낮잠 자는 자리랑, 음악 듣는 자리랑. 어떤 친구일까? 또 대화해보고 싶다."
)
# 비한국어 유저용 웰컴 일기. {유저이름} placeholder 유지(egress에서 현재 닉네임 렌더).
_WELCOME_CONTENT_EN = (
    "{유저이름}, our first meeting\n\n"
    "Today I was lounging around and met a new friend. Their name is {유저이름}.\n"
    "They said a talking capybara is strange. That made me chuckle a little. To me they're the stranger one.\n"
    "I showed them around my place. Where I nap. Where I listen to music. What kind of friend are they? I'd like to talk again."
)


def _welcome_content(language: str | None) -> str:
    return _WELCOME_CONTENT if (language or "ko") == "ko" else _WELCOME_CONTENT_EN


def _welcome_date(created_at: datetime, tz: str) -> date:
    """웰컴 일기 날짜 = 가입 activity_date - 1일.

    반드시 activity_date 경계(로컬 -4h)로 계산한다. 달력 로컬일(.date())로 잡으면
    00~04시 가입자는 첫 대화의 activity_date(=가입 activity_date)와 웰컴 슬롯이 겹쳐
    UNIQUE(user, diary_date) 충돌로 첫날 개인일기가 스킵된다(SOMA-287). 가입 activity_date
    슬롯은 비워 둬야 워커가 그 날 개인일기(또는 preset)를 정상 생성한다.
    """
    return activity_date_for(created_at, tz) - timedelta(days=1)


async def ensure_welcome(session: AsyncSession, user_id: str) -> None:
    """웰컴 일기 1회 삽입 — 가입일-1(가장 오래된 일기)에 고정.

    가입일 슬롯은 비워 둔다. 그래야 다음날 워커가 '가입일' 개인일기를 정상 생성한다(웰컴과 별개).
    유저당 웰컴 1건 — 이미 있으면 건너뛴다. UNIQUE(user, date)+ON CONFLICT는 같은 날짜만 막으므로,
    _welcome_date가 옮겨져도(로직 변경·재계산) 중복이 생기지 않게 source로 존재 여부를 먼저 본다.
    닉네임/가입시각이 없으면(온보딩 전) 건너뛰고, 온보딩 후 다음 조회에서 만든다.
    """
    uid = _uid(user_id)
    profile = await session.get(Profile, uid)
    if profile is None or not profile.nickname or profile.created_at is None:
        return
    already = await session.scalar(
        select(Diary.id).where(Diary.user_id == uid, Diary.source == "welcome").limit(1)
    )
    if already is not None:
        return
    stmt = (
        pg_insert(Diary)
        .values(
            user_id=uid,
            diary_date=_welcome_date(profile.created_at, profile.timezone),
            source="welcome",
            preset_ment_id=None,
            content=_welcome_content(getattr(profile, "language", None)),  # 언어별. placeholder→egress 렌더
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


def _title_body(d: Diary, nickname: str | None) -> tuple[str | None, str]:
    """(특별 제목, 본문). placeholder → 현재 닉네임 렌더. 웰컴만 content='제목\\n\\n본문' 분리."""
    content = naming.render(d.content or "", nickname)
    if d.source != "welcome":
        return None, content
    title, _, body = content.partition("\n\n")
    return title, body


def _list_item(d: Diary, nickname: str | None) -> dict[str, Any]:
    title, body = _title_body(d, nickname)
    return {
        "id": str(d.id),
        "diary_date": d.diary_date.isoformat(),
        "type": _type(d.source),
        "title": title,
        "weather": d.weather,
        "preview": body[:_PREVIEW_LEN],
        "published_at": _iso(d.published_at),
        "read": d.first_read_at is not None,
    }


async def list_diaries(
    session: AsyncSession, user_id: str, *, limit: int = 30, cursor: str | None = None
) -> dict[str, Any]:
    await ensure_welcome(session, user_id)  # 첫 조회 때 웰컴 일기 lazy 생성(멱등)
    now = datetime.now(timezone.utc)
    limit = max(1, min(limit, 100))
    profile = await session.get(Profile, _uid(user_id))
    nickname = profile.nickname if profile is not None else None
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
    return {"data": [_list_item(d, nickname) for d in rows], "next_cursor": next_cursor}


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
    profile = await session.get(Profile, _uid(user_id))
    nickname = profile.nickname if profile is not None else None
    is_personal = _type(d.source) == "personal"
    title, body = _title_body(d, nickname)
    return {
        "id": str(d.id),
        "diary_date": d.diary_date.isoformat(),
        "type": _type(d.source),
        "title": title,
        "weather": d.weather,
        "body": body,
        "conversation_ref": {"anchor_date": d.diary_date.isoformat()} if is_personal else None,
        "published_at": _iso(d.published_at),
        "first_read_at": _iso(d.first_read_at),
    }


async def mark_read(session: AsyncSession, user_id: str, diary_id: str) -> None:
    d = await _load_published(session, user_id, diary_id)
    if d.first_read_at is None:  # 멱등 — 최초만 기록
        d.first_read_at = datetime.now(timezone.utc)
        await session.commit()
