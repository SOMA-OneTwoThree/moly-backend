"""diary 서비스 — 조회·상세·열람표시. 첫 만남은 첫 조회, daily는 워커(04:00 배치)에서 생성. 열람은 등급무관 무료.

노출 규칙: published_at ≤ now 인 건만(배치 생성분의 발행 전 노출 방지, API_SPEC §4).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.time_utils import safe_zone
from app.models.diary import Diary
from app.models.profile import Profile
from app.services import i18n, naming, privacy
from app.services.account import _uid

_PREVIEW_LEN = 60
_VISIBLE_KINDS = ("welcome", "shared_day", "capi_day")

# 웰컴 일기는 대화 여부와 무관하게 가입 직후 첫 조회에서 생성한다.
# 실제 대화나 유저의 발언을 지어내지 않고, 이름은 조회 시 현재 닉네임으로 렌더한다.
_WELCOME_CONTENT = (
    "{유저이름}, 첫 만남\n\n"
    "오늘 새 친구 {유저이름}을 만났다.\n"
    "어떤 친구일까? 앞으로 함께할 날들이 궁금하다."
)
_WELCOME_CONTENT_EN = (
    "{유저이름}, our first meeting\n\n"
    "Today I met a new friend, {유저이름}.\n"
    "What kind of friend will they be? I look forward to our days together."
)


# 일본어 유저용 웰컴 일기. {유저이름} placeholder 유지(egress에서 현재 닉네임 렌더).
_WELCOME_CONTENT_JA = (
    "{유저이름}、はじめての出会い\n\n"
    "今日、新しい友だちの{유저이름}に出会った。\n"
    "どんな友だちなんだろう。これから一緒に過ごす日々が楽しみだ。"
)


def _welcome_content(language: str | None) -> str:
    bucket = i18n.resolve(language)
    if bucket == "ko":
        return _WELCOME_CONTENT
    if bucket == "ja":
        return _WELCOME_CONTENT_JA
    return _WELCOME_CONTENT_EN


def _welcome_date(created_at: datetime, tz: str) -> date:
    """가입 시각의 로컬 달력 날짜. 최초 저장 뒤 timezone이 바뀌어도 재계산하지 않는다."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(safe_zone(tz)).date()


def _welcome_parts(language: str | None) -> tuple[str, str]:
    title, separator, body = _welcome_content(language).partition("\n\n")
    if not separator:  # 상수 손상은 빈 본문을 발행하지 않고 즉시 드러낸다.
        raise RuntimeError("welcome diary template must contain a title and body")
    return title, body


async def ensure_welcome(session: AsyncSession, profile: Profile) -> uuid.UUID | None:
    """가입 기준 welcome을 멱등 삽입한다. 호출자가 조회 전에 커밋한다.

    닉네임·대화는 필수 조건이 아니다. 기존 일기는 날짜·언어를 바꾸지 않으며 삭제된 일기도
    재생성하지 않는다. 동시 첫 조회는 DB의 user당 active welcome 유니크 인덱스로 수렴한다.
    """
    if profile.created_at is None:
        return None
    existing = await session.scalar(
        select(Diary.id).where(Diary.user_id == profile.id, Diary.kind == "welcome").limit(1)
    )
    if existing is not None:
        return None
    created_at = profile.created_at
    created_at = (
        created_at.replace(tzinfo=timezone.utc)
        if created_at.tzinfo is None
        else created_at.astimezone(timezone.utc)
    )
    tz_name = profile.timezone
    display_date = _welcome_date(created_at, tz_name)
    title, body = _welcome_parts(profile.language)
    stmt = (
        pg_insert(Diary)
        .values(
            user_id=profile.id,
            diary_date=display_date,  # v1 compatibility alias
            kind="welcome",
            activity_date=None,
            display_date=display_date,
            title=title,
            author="capi",
            occurred_at=created_at,
            occurred_timezone=tz_name,
            occurred_timezone_provenance="profile_snapshot",
            primary_subject="user",
            about_tags=["user"],
            source="welcome",
            preset_ment_id=None,
            content=body,
            weather="sunny",
            published_at=created_at,
        )
        .on_conflict_do_nothing()
        .returning(Diary.id)
    )
    inserted = await session.scalar(stmt)
    if inserted is not None:
        from app.services import diary_recall_repo

        # 가입 환영 문구에는 대화 근거가 없다. 회상 문서·색인 잡만 같은 트랜잭션에 저장한다.
        await diary_recall_repo.upsert_diary_recall_document(
            session,
            user_id=profile.id,
            diary_id=inserted,
        )
    return inserted


def _kind(diary_or_source: Diary | str) -> str | None:
    if not isinstance(diary_or_source, str):
        value = getattr(diary_or_source, "kind", None)
        if value in _VISIBLE_KINDS:
            return value
        source = getattr(diary_or_source, "source", "")
    else:
        source = diary_or_source
    return {
        "welcome": "welcome",
        "llm": "shared_day",
        "preset": "capi_day",
        "shared_day": "shared_day",
        "capi_day": "capi_day",
    }.get(source)


def _type(source_or_kind: str) -> str:
    return "personal" if _kind(source_or_kind) == "shared_day" else "moly"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _title_body(d: Diary, nickname: str | None) -> tuple[str | None, str]:
    """title/body placeholder를 현재 닉네임으로 렌더한다. legacy welcome도 읽는다."""
    content = naming.render(d.content or "", nickname)
    stored_title = getattr(d, "title", None)
    if stored_title is not None:
        return naming.render(stored_title, nickname), content
    if _kind(d) == "welcome":
        title, separator, body = content.partition("\n\n")
        return (title, body) if separator else (None, content)
    return None, content


def _display_date(d: Diary) -> date:
    return getattr(d, "display_date", None) or d.diary_date


def _activity_date(d: Diary) -> date:
    return getattr(d, "activity_date", None) or _display_date(d)


def _list_item(d: Diary, nickname: str | None) -> dict[str, Any]:
    title, body = _title_body(d, nickname)
    return {
        "id": str(d.id),
        "diary_date": _display_date(d).isoformat(),
        "type": _type(_kind(d) or d.source),
        "title": title,
        "weather": d.weather,
        "preview": body[:_PREVIEW_LEN],
        "published_at": _iso(d.published_at),
        "read": d.first_read_at is not None,
    }


async def list_diaries(
    session: AsyncSession, user_id: str, *, limit: int = 30, cursor: str | None = None
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    limit = max(1, min(limit, 100))
    profile = await session.get(Profile, _uid(user_id))
    await privacy.ensure_subject_active(session, _uid(user_id))
    nickname = profile.nickname if profile is not None else None
    if profile is not None:
        inserted = await ensure_welcome(session, profile)
        if inserted is not None:
            await session.commit()
    q = select(Diary).where(
        Diary.user_id == _uid(user_id),
        Diary.record_status == "published",
        Diary.deleted_at.is_(None),
        Diary.published_at <= now,
        Diary.kind.in_(_VISIBLE_KINDS),
    )
    if cursor:
        try:
            cursor_date = date.fromisoformat(cursor)
        except ValueError as e:
            raise errors.validation("잘못된 커서 형식이에요.") from e
        q = q.where(Diary.display_date < cursor_date)
    # v1 date cursor는 같은 표시 날짜의 welcome+daily 중 하나를 건너뛸 수 있다. 경계 날짜
    # 동률을 함께 반환하므로 target limit보다 최대 한 건 많을 수 있다.
    q = q.order_by(Diary.display_date.desc(), Diary.id.desc()).limit(limit + 2)
    rows = list((await session.execute(q)).scalars().all())
    page = rows[:limit]
    boundary = _display_date(page[-1]) if page else None
    index = limit
    while boundary is not None and index < len(rows) and _display_date(rows[index]) == boundary:
        page.append(rows[index])
        index += 1
    has_more = index < len(rows)
    next_cursor = boundary.isoformat() if has_more and boundary is not None else None
    return {"data": [_list_item(d, nickname) for d in page], "next_cursor": next_cursor}


async def _load_published(session: AsyncSession, user_id: str, diary_id: str) -> Diary:
    await privacy.ensure_subject_active(session, _uid(user_id))
    try:
        did = uuid.UUID(diary_id)
    except ValueError as e:
        raise errors.AppError("NOT_FOUND", 404, "일기를 찾을 수 없어요.") from e
    d = await session.get(Diary, did)
    now = datetime.now(timezone.utc)
    if (
        d is None
        or d.user_id != _uid(user_id)
        or getattr(d, "record_status", "published") != "published"
        or getattr(d, "deleted_at", None) is not None
        or d.published_at is None
        or d.published_at > now
        or _kind(d) not in _VISIBLE_KINDS
    ):
        raise errors.AppError("NOT_FOUND", 404, "일기를 찾을 수 없어요.")
    return d


async def get_diary(session: AsyncSession, user_id: str, diary_id: str) -> dict[str, Any]:
    d = await _load_published(session, user_id, diary_id)
    profile = await session.get(Profile, _uid(user_id))
    nickname = profile.nickname if profile is not None else None
    kind = _kind(d)
    is_personal = kind == "shared_day"
    title, body = _title_body(d, nickname)
    return {
        "id": str(d.id),
        "diary_date": _display_date(d).isoformat(),
        "type": _type(kind or d.source),
        "title": title,
        "weather": d.weather,
        "body": body,
        "conversation_ref": {"anchor_date": _activity_date(d).isoformat()} if is_personal else None,
        "published_at": _iso(d.published_at),
        "first_read_at": _iso(d.first_read_at),
    }


async def mark_read(session: AsyncSession, user_id: str, diary_id: str) -> None:
    d = await _load_published(session, user_id, diary_id)
    if d.first_read_at is None:  # 멱등 — 최초만 기록
        d.first_read_at = datetime.now(timezone.utc)
        await session.commit()
