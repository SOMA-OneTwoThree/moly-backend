"""diary 서비스 — 조회·상세·열람표시. 생성은 워커(04:00 배치). 열람은 등급무관 무료.

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

# 웰컴 프롤로그는 첫 성공 대화의 Phase B에서 동기 생성한다. 가입 시각·목록 GET을 생성
# 트리거로 쓰지 않고, 커밋된 사실만 담는 결정적·사실 중립 템플릿을 사용한다.
_WELCOME_CONTENT = (
    "{유저이름}, 첫 만남\n\n"
    "오늘 {유저이름}과 처음 대화를 나눴다.\n"
    "우리의 첫 대화가 시작된 날이다."
)
_WELCOME_CONTENT_EN = (
    "{유저이름}, our first meeting\n\n"
    "Today, {유저이름} and I talked for the first time.\n"
    "This is the day our first conversation began."
)


# 일본어 유저용 웰컴 일기. {유저이름} placeholder 유지(egress에서 현재 닉네임 렌더).
_WELCOME_CONTENT_JA = (
    "{유저이름}、はじめての出会い\n\n"
    "今日、{유저이름}とはじめて話した。\n"
    "わたしたちの最初の会話が始まった日だ。"
)


def _welcome_content(language: str | None) -> str:
    bucket = i18n.resolve(language)
    if bucket == "ko":
        return _WELCOME_CONTENT
    if bucket == "ja":
        return _WELCOME_CONTENT_JA
    return _WELCOME_CONTENT_EN


def _welcome_date(started_at: datetime, tz: str) -> date:
    """첫 커밋 대화의 당시 로컬 달력 날짜. timezone 변경 뒤에도 저장값은 바뀌지 않는다."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at.astimezone(safe_zone(tz)).date()


def _welcome_parts(language: str | None) -> tuple[str, str]:
    title, separator, body = _welcome_content(language).partition("\n\n")
    if not separator:  # 상수 손상은 빈 본문을 발행하지 않고 즉시 드러낸다.
        raise RuntimeError("welcome diary template must contain a title and body")
    return title, body


async def ensure_welcome_for_first_committed_turn(
    session: AsyncSession,
    profile: Profile,
    started_at: datetime,
    *,
    source_message_id: int | None = None,
) -> uuid.UUID | None:
    """첫 성공 대화와 같은 Phase B 트랜잭션에 welcome 프롤로그를 멱등 삽입한다.

    commit하지 않는다. 호출자가 user/assistant message, relationship_started_at과 원자적으로 확정한다.
    partial unique ``one welcome per user``가 동시 삽입을 수렴시키며, 목록 GET은 이 함수를 호출하지
    않는다. 반환값은 이번 호출에서 새로 삽입된 diary id이고 기존 행이면 ``None``이다.
    """

    started_at = (
        started_at.replace(tzinfo=timezone.utc)
        if started_at.tzinfo is None
        else started_at.astimezone(timezone.utc)
    )
    tz_name = getattr(profile, "relationship_started_timezone", None) or profile.timezone
    display_date = _welcome_date(started_at, tz_name)
    title, body = _welcome_parts(getattr(profile, "language", None))
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
            occurred_at=started_at,
            occurred_timezone=tz_name,
            occurred_timezone_provenance="profile_snapshot",
            primary_subject="user",
            about_tags=["user"],
            source="welcome",
            preset_ment_id=None,
            content=body,
            weather="sunny",
            published_at=started_at,
        )
        .on_conflict_do_nothing()
        .returning(Diary.id)
    )
    inserted = await session.scalar(stmt)
    diary_id = inserted
    if diary_id is None:
        diary_id = await session.scalar(
            select(Diary.id).where(
                Diary.user_id == profile.id,
                Diary.kind == "welcome",
            )
        )
    if inserted is not None:
        # Import here so expand-reader deployments can load the service before the new projection
        # module is activated. Both hooks join the caller's Phase B transaction.
        from app.services import diary_recall_repo

        if source_message_id is None:
            from app.models.message import Message

            source_message_id = await session.scalar(
                select(Message.id)
                .where(Message.user_id == profile.id, Message.sender == "user")
                .order_by(Message.id)
                .limit(1)
            )
        if source_message_id is not None:
            await diary_recall_repo.record_diary_sources(
                session,
                user_id=profile.id,
                diary_id=diary_id,
                message_ids=[source_message_id],
            )
        await diary_recall_repo.upsert_diary_recall_document(
            session,
            user_id=profile.id,
            diary_id=diary_id,
        )
    if hasattr(profile, "relationship_started_at") and profile.relationship_started_at is None:
        profile.relationship_started_at = started_at
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
