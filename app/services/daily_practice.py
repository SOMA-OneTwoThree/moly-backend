"""오늘의 작은 실천 — 공용 풀에서 하루 하나를 제안하고 수락·완료·루틴 추가를 처리한다.

요청 경로는 LLM을 부르지 않는다. 앱이 보내는 `focus`는 선택에만 쓰고 저장하지 않는다.
사용자 행은 프로필 CASCADE로 사라진다. 변경 경로는 `begin_subject_deletion`과 같은 advisory lock을
잡은 뒤 삭제 장벽을 확인하므로 장벽이 선 뒤에는 행이 새로 생기지 않는다.
"""
from __future__ import annotations

import contextlib
import logging
import random
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.advisory_lock import advisory_xact_lock
from app.core.app_day import AppDay, validate_app_timezone
from app.core.errors import AppError
from app.core.time_utils import safe_zone
from app.models.daily_practice import DailyPracticeAction, DailyPracticeOffer
from app.models.mood import MoodEntry
from app.models.routine import Routine, RoutineCompletion, RoutineSkip
from app.services import daily_practice_selection as selection
from app.services import jobs, privacy, routine
from app.services.account import _load_profile, _uid
from app.services.i18n import resolve

_log = logging.getLogger("moly-backend")

JOB_TOP_UP = "daily_practice_top_up"

OFFERS_PER_DAY = 3
HISTORY_DAYS = 60
MOOD_DAYS = 3

OFFERED, SKIPPED, ACCEPTED, COMPLETED = "offered", "skipped", "accepted", "completed"

ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]
ROUTINE_COLORS = {
    "rest": "lavender",
    "body": "green",
    "calm": "blue",
    "mind": "mint",
    "self_kindness": "pink",
    "connect": "peach",
    "order": "yellow",
}


def _invalid_state() -> AppError:
    return AppError("DAILY_PRACTICE_INVALID_STATE", 409, "지금은 할 수 없는 동작이에요.")


def _clock(now_utc: datetime | None) -> datetime:
    return now_utc or datetime.now(timezone.utc)


async def _day(
    session: AsyncSession, user_id: str, timezone_name: str | None, now: datetime
) -> tuple[AppDay, int]:
    if timezone_name is None:
        timezone_name = (await _load_profile(session, user_id)).timezone
    return AppDay.at(now, timezone_name), now.astimezone(safe_zone(timezone_name)).hour


async def _current(
    session: AsyncSession, uid: uuid.UUID, now: datetime
) -> DailyPracticeOffer | None:
    return (await session.execute(
        select(DailyPracticeOffer)
        .where(
            DailyPracticeOffer.user_id == uid,
            DailyPracticeOffer.status != SKIPPED,
            DailyPracticeOffer.expires_at > now,
        )
        .order_by(DailyPracticeOffer.created_at.desc())
        .limit(1)
    )).scalars().first()


async def _rows_on(
    session: AsyncSession, uid: uuid.UUID, local_date: date
) -> list[DailyPracticeOffer]:
    return list((await session.execute(
        select(DailyPracticeOffer).where(
            DailyPracticeOffer.user_id == uid, DailyPracticeOffer.local_date == local_date
        )
    )).scalars().all())


async def _practice(
    session: AsyncSession, row: DailyPracticeOffer, language: str
) -> dict[str, Any]:
    action = await session.get(DailyPracticeAction, row.action_id)
    return {
        "id": row.id,
        "status": row.status,
        "category": action.category,
        "title": getattr(action, f"title_{language}"),
        "speech": getattr(action, f"speech_{language}"),
        "icon": action.icon,
        "routine_added": row.routine_id is not None,
    }


async def _today(
    session: AsyncSession, uid: uuid.UUID, day: AppDay, language: str, now: datetime
) -> dict[str, Any]:
    current = await _current(session, uid, now)
    rows = await _rows_on(session, uid, current.local_date if current else day.local_date)
    if current is None:
        # 시간대가 바뀌면 만료된 행과 같은 현지 날짜일 수 있다. 그날은 더 제안하지 않는다.
        expired_today = any(row.status != SKIPPED for row in rows)
        remaining = 0 if expired_today else OFFERS_PER_DAY - 1 - len(rows)
    elif current.status == OFFERED:
        remaining = OFFERS_PER_DAY - len(rows)
    else:
        remaining = 0
    return {
        "local_date": current.local_date if current else day.local_date,
        "expires_at": current.expires_at if current else day.ends_at,
        "locale": language,
        "practice": await _practice(session, current, language) if current else None,
        "offers_remaining": max(0, remaining),
    }


async def _make_offer(
    session: AsyncSession,
    uid: uuid.UUID,
    day: AppDay,
    hour: int,
    focus: list[str],
    rng: random.Random | None,
) -> tuple[str, tuple[str, ...]]:
    """오늘 제안이 비어 있으면 하나 만든다. 반환은 (시간대, 보충을 요청할 카테고리)."""
    slot = selection.slot_of(hour)
    today_rows = await _rows_on(session, uid, day.local_date)
    if len(today_rows) >= OFFERS_PER_DAY or any(row.status != SKIPPED for row in today_rows):
        return slot, ()
    actions = (await session.execute(select(DailyPracticeAction))).scalars().all()
    by_id = {action.id: action for action in actions}
    rows = (await session.execute(
        select(DailyPracticeOffer).where(
            DailyPracticeOffer.user_id == uid,
            DailyPracticeOffer.local_date >= day.local_date - timedelta(days=HISTORY_DAYS),
        )
    )).scalars().all()
    moods = (await session.execute(
        select(MoodEntry.kind).where(
            MoodEntry.user_id == uid,
            MoodEntry.entry_date > day.local_date - timedelta(days=MOOD_DAYS),
            MoodEntry.entry_date <= day.local_date,
        )
    )).scalars().all()
    picked = selection.choose(
        [action for action in actions if action.status == "active"],
        slot=slot,
        today=day.local_date,
        history=[
            selection.Shown(row.action_id, by_id[row.action_id].category, row.status, row.local_date)
            for row in rows
        ],
        focus=focus,
        moods=moods,
        rng=rng or random.Random(),
    )
    if picked.action is not None:
        session.add(DailyPracticeOffer(
            id=uuid.uuid4(), user_id=uid, action_id=picked.action.id, local_date=day.local_date,
            expires_at=day.ends_at, status=OFFERED,
        ))
        await session.flush()
    return slot, picked.short_categories


async def _request_top_up(
    session: AsyncSession, categories: tuple[str, ...], slot: str, now: datetime
) -> None:
    """응답을 확정한 뒤 부른다. 실패해도 사용자 요청에는 아무 영향이 없다."""
    if not categories:
        return
    utc_date = now.astimezone(timezone.utc).date().isoformat()
    try:
        for category in categories:
            await jobs.enqueue(
                session,
                queue=jobs.QUEUE_MAINTENANCE,
                job_type=JOB_TOP_UP,
                dedup_key=f"{JOB_TOP_UP}:{category}:{utc_date}",
                payload={"category": category, "slot": slot},
            )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — 보충은 부가 작업이다
        _log.warning("daily_practice_top_up_enqueue_failed %s", type(exc).__name__)
        with contextlib.suppress(Exception):
            await session.rollback()


async def _enter(session: AsyncSession, user_id: str, timezone_name: str | None) -> uuid.UUID:
    """변경 경로 진입: 시간대 검증, 사용자 직렬화, 삭제 장벽 확인."""
    validate_app_timezone(timezone_name)
    uid = _uid(user_id)
    await advisory_xact_lock(session, uid)
    await privacy.ensure_subject_active(session, uid)
    return uid


async def _owned(session: AsyncSession, uid: uuid.UUID, practice_id: str) -> DailyPracticeOffer:
    not_found = AppError("DAILY_PRACTICE_NOT_FOUND", 404, "오늘의 작은 실천을 찾을 수 없어요.")
    try:
        offer_id = uuid.UUID(practice_id)
    except ValueError as exc:
        raise not_found from exc
    row = (await session.execute(
        select(DailyPracticeOffer).where(
            DailyPracticeOffer.id == offer_id, DailyPracticeOffer.user_id == uid
        )
    )).scalars().first()
    if row is None:
        raise not_found
    return row


def _require_live(row: DailyPracticeOffer, now: datetime) -> None:
    if row.expires_at <= now:
        raise AppError("DAILY_PRACTICE_EXPIRED", 409, "오늘의 작은 실천이 이미 끝났어요.")


async def status(
    session: AsyncSession,
    user_id: str,
    *,
    locale: str | None,
    timezone_name: str | None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    uid = _uid(user_id)
    await privacy.ensure_subject_active(session, uid)
    validate_app_timezone(timezone_name)
    now = _clock(now_utc)
    day, _ = await _day(session, user_id, timezone_name, now)
    return await _today(session, uid, day, resolve(locale), now)


async def offer(
    session: AsyncSession,
    user_id: str,
    *,
    focus: list[str],
    locale: str | None,
    timezone_name: str | None,
    now_utc: datetime | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    uid = await _enter(session, user_id, timezone_name)
    now = _clock(now_utc)
    day, hour = await _day(session, user_id, timezone_name, now)
    slot, short = selection.slot_of(hour), ()
    if await _current(session, uid, now) is None:
        slot, short = await _make_offer(session, uid, day, hour, focus, rng)
    result = await _today(session, uid, day, resolve(locale), now)
    await session.commit()
    await _request_top_up(session, short, slot, now)
    return result


async def skip(
    session: AsyncSession,
    user_id: str,
    practice_id: str,
    *,
    focus: list[str],
    locale: str | None,
    timezone_name: str | None,
    now_utc: datetime | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    uid = await _enter(session, user_id, timezone_name)
    now = _clock(now_utc)
    row = await _owned(session, uid, practice_id)
    _require_live(row, now)
    if row.status != OFFERED:
        raise _invalid_state()
    if len(await _rows_on(session, uid, row.local_date)) >= OFFERS_PER_DAY:
        raise AppError("DAILY_PRACTICE_OFFER_LIMIT", 409, "오늘은 더 바꿀 수 없어요.")
    day, hour = await _day(session, user_id, timezone_name, now)
    row.status, row.updated_at = SKIPPED, now
    await session.flush()
    slot, short = await _make_offer(session, uid, day, hour, focus, rng)
    result = await _today(session, uid, day, resolve(locale), now)
    await session.commit()
    await _request_top_up(session, short, slot, now)
    return result


async def _transition(
    session: AsyncSession,
    user_id: str,
    practice_id: str,
    apply: Callable[[DailyPracticeOffer], None],
    *,
    locale: str | None,
    timezone_name: str | None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    uid = await _enter(session, user_id, timezone_name)
    now = _clock(now_utc)
    row = await _owned(session, uid, practice_id)
    _require_live(row, now)
    day, _ = await _day(session, user_id, timezone_name, now)
    before = row.status
    apply(row)
    if row.status != before:
        row.updated_at = now
    await session.flush()
    result = await _today(session, uid, day, resolve(locale), now)
    await session.commit()
    return result


def _accept(row: DailyPracticeOffer) -> None:
    if row.status not in {OFFERED, ACCEPTED}:
        raise _invalid_state()
    row.status = ACCEPTED


def _complete(row: DailyPracticeOffer) -> None:
    if row.status not in {ACCEPTED, COMPLETED}:
        raise _invalid_state()
    row.status = COMPLETED


def _uncomplete(row: DailyPracticeOffer) -> None:
    if row.status not in {COMPLETED, ACCEPTED} or row.routine_id is not None:
        raise _invalid_state()
    row.status = ACCEPTED


async def accept(session: AsyncSession, user_id: str, practice_id: str, **kwargs: Any) -> dict[str, Any]:
    return await _transition(session, user_id, practice_id, _accept, **kwargs)


async def complete(session: AsyncSession, user_id: str, practice_id: str, **kwargs: Any) -> dict[str, Any]:
    return await _transition(session, user_id, practice_id, _complete, **kwargs)


async def uncomplete(session: AsyncSession, user_id: str, practice_id: str, **kwargs: Any) -> dict[str, Any]:
    return await _transition(session, user_id, practice_id, _uncomplete, **kwargs)


async def add_routine(
    session: AsyncSession,
    user_id: str,
    practice_id: str,
    *,
    locale: str | None,
    timezone_name: str | None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    uid = await _enter(session, user_id, timezone_name)
    now = _clock(now_utc)
    language = resolve(locale)
    row = await _owned(session, uid, practice_id)
    _require_live(row, now)
    if row.status != COMPLETED:
        raise _invalid_state()
    day, _ = await _day(session, user_id, timezone_name, now)
    if row.routine_id is not None:
        added = await session.get(Routine, row.routine_id)
        routine_dto = routine._dto(
            added,
            await _recorded_today(session, RoutineCompletion, uid, added.id, day.local_date),
            language,
            await _recorded_today(session, RoutineSkip, uid, added.id, day.local_date),
        )
    else:
        action = await session.get(DailyPracticeAction, row.action_id)
        added = Routine(
            id=uuid.uuid4(), user_id=uid,
            name=getattr(action, f"title_{language}"),
            name_i18n={"ko": action.title_ko, "en": action.title_en, "ja": action.title_ja},
            icon=action.icon, color=ROUTINE_COLORS[action.category],
            frequency_per_week=len(ALL_DAYS), days_of_week=ALL_DAYS,
            reminder_enabled=False, reminder_time=None,
        )
        await routine.add_with_schedule(session, added, day.local_date)
        row.routine_id, row.updated_at = added.id, now
        await session.flush()
        routine_dto = routine._dto(added, False, language)
    result = {"today": await _today(session, uid, day, language, now), "routine": routine_dto}
    await session.commit()
    return result


async def _recorded_today(
    session: AsyncSession, model: Any, uid: uuid.UUID, routine_id: uuid.UUID, local_date: date
) -> bool:
    return (await session.execute(
        select(model.routine_id).where(
            model.user_id == uid, model.routine_id == routine_id, model.activity_date == local_date
        )
    )).scalars().first() is not None
