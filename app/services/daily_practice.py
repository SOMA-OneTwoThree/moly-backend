"""오늘의 작은 실천 — 공용 템플릿에서 하루 3개를 제안하고 선택, 완료, 루틴 추가를 처리한다.

사용자 행은 프로필 CASCADE로 사라진다. 변경 경로는 `begin_subject_deletion`과 같은 advisory lock을
잡은 뒤 삭제 장벽을 확인하므로 장벽이 선 뒤에는 행이 새로 생기지 않는다.
"""
from __future__ import annotations

import random
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.advisory_lock import advisory_xact_lock
from app.core.app_day import AppDay, validate_app_timezone
from app.core.errors import AppError
from app.models.daily_practice import DailyPracticeOffer, DailyPracticeTemplate
from app.models.routine import Routine, RoutineCompletion, RoutineSkip
from app.services import privacy, routine
from app.services.account import _load_profile, _uid
from app.services.i18n import resolve

OPTIONS_PER_DAY = 3
AVOID_DAYS = 30
DEADLINE = timedelta(hours=24)

OFFERED, ACCEPTED, COMPLETED = "offered", "accepted", "completed"

ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]
ROUTINE_ICON = "seedling"
ROUTINE_COLOR = "peach"


def pick_options(
    template_ids: Sequence[uuid.UUID], last_shown: Mapping[uuid.UUID, date], rng: random.Random
) -> list[uuid.UUID]:
    """최근에 안 보인 것에서 무작위로 고르고, 모자라면 가장 오래전에 보인 순으로 채운다."""
    fresh = [t for t in template_ids if t not in last_shown]
    recent = [t for t in template_ids if t in last_shown]
    picked = rng.sample(fresh, min(OPTIONS_PER_DAY, len(fresh)))
    rng.shuffle(recent)
    recent.sort(key=last_shown.__getitem__)
    return picked + recent[:OPTIONS_PER_DAY - len(picked)]


@dataclass(frozen=True)
class _Today:
    live: DailyPracticeOffer | None
    practice: DailyPracticeOffer | None
    can_choose: bool
    options: list[DailyPracticeOffer]


def _invalid_state() -> AppError:
    return AppError("DAILY_PRACTICE_INVALID_STATE", 409, "지금은 할 수 없는 동작이에요.")


def _expired() -> AppError:
    return AppError("DAILY_PRACTICE_EXPIRED", 409, "오늘의 작은 실천이 이미 끝났어요.")


def _clock(now_utc: datetime | None) -> datetime:
    return now_utc or datetime.now(timezone.utc)


async def _day(
    session: AsyncSession, user_id: str, timezone_name: str | None, now: datetime
) -> AppDay:
    if timezone_name is None:
        timezone_name = (await _load_profile(session, user_id)).timezone
    return AppDay.at(now, timezone_name)


async def _load_today(
    session: AsyncSession, uid: uuid.UUID, day: AppDay, now: datetime
) -> _Today:
    rows = (await session.execute(
        select(DailyPracticeOffer).where(
            DailyPracticeOffer.user_id == uid,
            or_(
                DailyPracticeOffer.local_date == day.local_date,
                DailyPracticeOffer.completed_local_date == day.local_date,
                and_(DailyPracticeOffer.status == ACCEPTED, DailyPracticeOffer.deadline > now),
            ),
        )
    )).scalars().all()
    live = next((r for r in rows if r.status == ACCEPTED and r.deadline > now), None)
    chosen_today = any(r.status != OFFERED and r.local_date == day.local_date for r in rows)
    can_choose = live is None and not chosen_today
    completed = [
        r for r in rows if r.status == COMPLETED and r.completed_local_date == day.local_date
    ]
    options = [
        r for r in rows
        if r.status == OFFERED and r.local_date == day.local_date and r.options_expire_at > now
    ]
    return _Today(
        live=live,
        practice=live or max(completed, key=lambda r: r.local_date, default=None),
        can_choose=can_choose,
        options=sorted(options, key=lambda r: r.id) if can_choose else [],
    )


async def _linked_routine(session: AsyncSession, row: DailyPracticeOffer) -> Routine | None:
    if row.routine_id is None:
        return None
    linked = await session.get(Routine, row.routine_id)
    return linked if linked is not None and linked.deleted_at is None else None


async def _practice(
    session: AsyncSession, row: DailyPracticeOffer, language: str
) -> dict[str, Any]:
    template = await session.get(DailyPracticeTemplate, row.template_id)
    return {
        "id": row.id,
        "status": row.status,
        "title": getattr(template, f"title_{language}"),
        "routine_added": await _linked_routine(session, row) is not None,
        "deadline": row.deadline,
    }


async def _response(
    session: AsyncSession, today: _Today, day: AppDay, language: str
) -> dict[str, Any]:
    return {
        "local_date": day.local_date,
        "expires_at": today.live.deadline if today.live else day.ends_at,
        "locale": language,
        "can_choose": today.can_choose,
        "options": [await _practice(session, row, language) for row in today.options],
        "practice": await _practice(session, today.practice, language) if today.practice else None,
    }


async def _create_options(
    session: AsyncSession, uid: uuid.UUID, day: AppDay, rng: random.Random
) -> None:
    templates = (await session.execute(select(DailyPracticeTemplate))).scalars().all()
    shown = (await session.execute(
        select(DailyPracticeOffer).where(
            DailyPracticeOffer.user_id == uid,
            DailyPracticeOffer.local_date >= day.local_date - timedelta(days=AVOID_DAYS),
        )
    )).scalars().all()
    last_shown: dict[uuid.UUID, date] = {}
    for row in shown:
        last_shown[row.template_id] = max(row.local_date, last_shown.get(row.template_id, row.local_date))
    shown_today = {row.template_id for row in shown if row.local_date == day.local_date}
    candidates = [t.id for t in templates if t.is_active and t.id not in shown_today]
    for template_id in pick_options(candidates, last_shown, rng):
        session.add(DailyPracticeOffer(
            id=uuid.uuid4(), user_id=uid, template_id=template_id, local_date=day.local_date,
            status=OFFERED, options_expire_at=day.ends_at,
        ))
    await session.flush()


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
    day = await _day(session, user_id, timezone_name, now)
    return await _response(session, await _load_today(session, uid, day, now), day, resolve(locale))


async def offer(
    session: AsyncSession,
    user_id: str,
    *,
    locale: str | None,
    timezone_name: str | None,
    now_utc: datetime | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    uid = await _enter(session, user_id, timezone_name)
    now = _clock(now_utc)
    day = await _day(session, user_id, timezone_name, now)
    today = await _load_today(session, uid, day, now)
    if today.can_choose and not today.options:
        await _create_options(session, uid, day, rng or random.Random())
        today = await _load_today(session, uid, day, now)
    result = await _response(session, today, day, resolve(locale))
    await session.commit()
    return result


Apply = Callable[[AsyncSession, DailyPracticeOffer, AppDay, datetime], Awaitable[None]]


async def _transition(
    session: AsyncSession,
    user_id: str,
    practice_id: str,
    apply: Apply,
    *,
    locale: str | None,
    timezone_name: str | None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    uid = await _enter(session, user_id, timezone_name)
    now = _clock(now_utc)
    day = await _day(session, user_id, timezone_name, now)
    row = await _owned(session, uid, practice_id)
    await apply(session, row, day, now)
    await session.flush()
    result = await _response(session, await _load_today(session, uid, day, now), day, resolve(locale))
    await session.commit()
    return result


async def _accept(
    session: AsyncSession, row: DailyPracticeOffer, day: AppDay, now: datetime
) -> None:
    if row.status == ACCEPTED and row.deadline > now:
        return
    today = await _load_today(session, row.user_id, day, now)
    if row not in today.options:
        raise _invalid_state()
    row.status, row.deadline, row.updated_at = ACCEPTED, now + DEADLINE, now


async def _complete(
    session: AsyncSession, row: DailyPracticeOffer, day: AppDay, now: datetime
) -> None:
    if row.status == COMPLETED:
        return
    if row.status != ACCEPTED:
        raise _invalid_state()
    if row.deadline <= now:
        raise _expired()
    row.status, row.completed_local_date, row.updated_at = COMPLETED, day.local_date, now


async def _chosen_since(session: AsyncSession, row: DailyPracticeOffer) -> bool:
    return (await session.execute(
        select(DailyPracticeOffer.id).where(
            DailyPracticeOffer.user_id == row.user_id,
            DailyPracticeOffer.status != OFFERED,
            DailyPracticeOffer.deadline > row.deadline,
        ).limit(1)
    )).scalars().first() is not None


async def _uncomplete(
    session: AsyncSession, row: DailyPracticeOffer, day: AppDay, now: datetime
) -> None:
    if row.status == OFFERED:
        raise _invalid_state()
    if row.status == COMPLETED and (
        await _linked_routine(session, row) is not None or await _chosen_since(session, row)
    ):
        raise _invalid_state()
    if row.deadline <= now:
        raise _expired()
    if row.status == COMPLETED:
        row.status, row.completed_local_date, row.routine_id, row.updated_at = ACCEPTED, None, None, now


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
    day = await _day(session, user_id, timezone_name, now)
    row = await _owned(session, uid, practice_id)
    if row.status != COMPLETED:
        raise _invalid_state()
    linked = await _linked_routine(session, row)
    if linked is not None:
        routine_dto = routine._dto(
            linked,
            await _recorded_today(session, RoutineCompletion, uid, linked.id, day.local_date),
            language,
            await _recorded_today(session, RoutineSkip, uid, linked.id, day.local_date),
        )
    else:
        template = await session.get(DailyPracticeTemplate, row.template_id)
        added = Routine(
            id=uuid.uuid4(), user_id=uid,
            name=getattr(template, f"title_{language}"),
            name_i18n={"ko": template.title_ko, "en": template.title_en, "ja": template.title_ja},
            icon=ROUTINE_ICON, color=ROUTINE_COLOR,
            frequency_per_week=len(ALL_DAYS), days_of_week=ALL_DAYS,
            reminder_enabled=False, reminder_time=None,
        )
        await routine.add_with_schedule(session, added, day.local_date)
        row.routine_id, row.updated_at = added.id, now
        await session.flush()
        routine_dto = routine._dto(added, False, language)
    today = await _load_today(session, uid, day, now)
    result = {"today": await _response(session, today, day, language), "routine": routine_dto}
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
