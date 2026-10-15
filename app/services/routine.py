"""루틴 — CRUD(soft delete)·완료 체크·통계·템플릿. 알림은 클라 로컬(서버는 스케줄 데이터만)."""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.advisory_lock import advisory_xact_lock
from app.core.app_day import AppDay
from app.core.time_utils import current_reward_date, safe_zone
from app.models.profile import Profile
from app.models.routine import (
    Routine,
    RoutineCompletion,
    RoutineSchedule,
    RoutineSkip,
    RoutineTemplate,
    RoutineTemplateCategory,
)
from app.services import i18n, routine_records
from app.services.account import _load_profile, _uid
from app.services.routine_records import RoutineRecord

STATS_MAX_DAYS = 371


def _streak(ad: date, done: set[date]) -> int:
    """오늘(미완료면 어제)부터 뒤로 연속 완료 일수(지정 요일 무관, 단순 달력일 연속).

    오늘이 아직 미완료여도 어제까지의 연속은 유지한다 — 하루가 안 끝났으므로. 오늘부터 역산하면
    오늘 미완료 시 0이 되어, 완료→취소가 streak을 0으로 떨구던 버그(SOMA-312)가 났다.
    진짜로 끊기는 건 어제도 비어 있을 때뿐.
    """
    cursor = ad if ad in done else ad - timedelta(days=1)
    n = 0
    while cursor in done:
        n += 1
        cursor -= timedelta(days=1)
    return n


def _dto(
    r: Routine, completed_today: bool, language: str | None = None, skipped_today: bool = False
) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "name": i18n.localized_name(r.name_i18n, language, r.name, kind="routine", key=str(r.id)),
        "icon": r.icon,
        "color": r.color,
        "template_id": r.template_id,
        "frequency_per_week": len(r.days_of_week),  # 하위호환 필드 — 항상 요일 수
        "days_of_week": r.days_of_week,
        "reminder_enabled": r.reminder_enabled,
        "reminder_time": r.reminder_time.strftime("%H:%M") if r.reminder_time else None,
        "completed_today": completed_today,
        "skipped_today": skipped_today,
    }


def _week_bounds(ad):
    """ad(로컬 activity_date)가 속한 주의 월~일 경계(ISO, 월요일 시작)."""
    monday = ad - timedelta(days=ad.isoweekday() - 1)
    return monday, monday + timedelta(days=6)


async def _today(session: AsyncSession, user_id: str, day: AppDay | None = None):
    if day is not None:
        return _uid(user_id), day.local_date
    profile = await _load_profile(session, user_id)
    return profile.id, current_reward_date(profile.timezone)


async def _load_owned(session: AsyncSession, uid: uuid.UUID, routine_id: str) -> Routine:
    try:
        rid = uuid.UUID(routine_id)
    except ValueError as e:
        raise errors.AppError("NOT_FOUND", 404, "루틴을 찾을 수 없어요.") from e
    r = await session.get(Routine, rid)
    if r is None or r.user_id != uid or r.deleted_at is not None:
        raise errors.AppError("NOT_FOUND", 404, "루틴을 찾을 수 없어요.")
    return r


def _local_date(value: datetime | None, zone: ZoneInfo) -> date | None:
    return value.astimezone(zone).date() if value else None


async def _all_routines(session: AsyncSession, uid: uuid.UUID) -> list[Routine]:
    return list((await session.execute(
        select(Routine).where(Routine.user_id == uid).order_by(Routine.created_at, Routine.id)
    )).scalars().all())


async def _routine_records(
    session: AsyncSession, profile: Profile, rows: list[Routine], until: date
) -> dict[uuid.UUID, RoutineRecord]:
    """요일 이력과 until까지의 완료·스킵. 이력·deleted_on이 없으면 프로필 시간대의 created_at·deleted_at 날짜."""
    if not rows:
        return {}
    stored: defaultdict[uuid.UUID, list[tuple[date, list[int]]]] = defaultdict(list)
    for routine_id, effective_from, days in (await session.execute(
        select(RoutineSchedule.routine_id, RoutineSchedule.effective_from, RoutineSchedule.days_of_week)
        .where(RoutineSchedule.routine_id.in_([r.id for r in rows]))
    )).all():
        stored[routine_id].append((effective_from, days))
    completed: defaultdict[uuid.UUID, set[date]] = defaultdict(set)
    for routine_id, activity_date in (await session.execute(
        select(RoutineCompletion.routine_id, RoutineCompletion.activity_date).where(
            RoutineCompletion.user_id == profile.id, RoutineCompletion.activity_date <= until,
        )
    )).all():
        completed[routine_id].add(activity_date)
    skips: defaultdict[uuid.UUID, set[date]] = defaultdict(set)
    for routine_id, activity_date in (await session.execute(
        select(RoutineSkip.routine_id, RoutineSkip.activity_date).where(
            RoutineSkip.user_id == profile.id, RoutineSkip.activity_date <= until,
        )
    )).all():
        skips[routine_id].add(activity_date)
    zone = safe_zone(profile.timezone)
    return {
        r.id: RoutineRecord(
            versions=routine_records.schedule(
                stored[r.id], r.days_of_week,
                r.created_at.astimezone(zone).date(), _local_date(r.updated_at, zone),
            ),
            end=r.deleted_on or _local_date(r.deleted_at, zone),
            completed=frozenset(completed[r.id]),
            skips=frozenset(skips[r.id]),
        )
        for r in rows
    }


async def list_routines(session: AsyncSession, user_id: str, day: AppDay | None = None) -> dict[str, Any]:
    profile = await _load_profile(session, user_id)
    uid, ad = profile.id, day.local_date if day else current_reward_date(profile.timezone)
    rows = list(
        (
            await session.execute(
                select(Routine)
                .where(Routine.user_id == uid, Routine.deleted_at.is_(None))
                .order_by(Routine.created_at, Routine.id)
            )
        ).scalars().all()
    )
    done = set(
        (
            await session.execute(
                select(RoutineCompletion.routine_id).where(
                    RoutineCompletion.user_id == uid, RoutineCompletion.activity_date == ad
                )
            )
        ).scalars().all()
    )
    skipped = set((await session.execute(
        select(RoutineSkip.routine_id).where(RoutineSkip.user_id == uid, RoutineSkip.activity_date == ad)
    )).scalars().all()) - done
    return {"data": [_dto(r, r.id in done, profile.language, r.id in skipped) for r in rows]}


async def create_routine(session: AsyncSession, user_id: str, req, day: AppDay) -> dict[str, Any]:
    uid = _uid(user_id)
    r = Routine(
        id=uuid.uuid4(), user_id=uid, name=req.name, icon=req.icon, color=req.color,
        frequency_per_week=len(req.days_of_week), days_of_week=req.days_of_week,
        reminder_enabled=req.reminder_enabled, reminder_time=req.reminder_time,
    )
    session.add(r)
    await session.flush()
    session.add(RoutineSchedule(
        routine_id=r.id, user_id=uid, effective_from=day.local_date, days_of_week=req.days_of_week,
    ))
    await session.commit()
    await session.refresh(r)
    return _dto(r, completed_today=False)


async def history(
    session: AsyncSession,
    user_id: str,
    selected_date: date,
    day: AppDay,
) -> dict[str, Any]:
    """선택일에 예정이었거나(스킵 포함) 완료한 루틴. 나중에 삭제된 루틴도 포함한다. 읽기 전용."""
    profile = await _load_profile(session, user_id)
    if selected_date > day.local_date:
        raise errors.AppError("VALIDATION", 422, "미래 날짜는 조회할 수 없어요.")
    rows = await _all_routines(session, profile.id)
    records = await _routine_records(session, profile, rows, selected_date)
    week_start, _ = _week_bounds(selected_date)
    data = []
    for row in rows:
        record = records[row.id]
        dates = record.completed
        completed = selected_date in dates
        if not completed and not record.planned(selected_date):
            continue
        week_dates = {d for d in dates if week_start <= d <= selected_date}
        metadata = _dto(row, completed, profile.language)
        data.append({
            key: metadata[key]
            for key in ("id", "name", "icon", "color", "reminder_enabled", "reminder_time")
        } | {
            "days_of_week": list(record.days_on(selected_date)),
            "completed": completed,
            "skipped": record.skipped(selected_date),
            "streak": _streak(selected_date, set(dates)) if selected_date > date.min else int(completed),
            "this_week": {
                "completed_count": len(week_dates),
                "by_weekday": {
                    str(i): any(d.isoweekday() == i for d in week_dates) for i in range(1, 8)
                },
            },
        })
    return {"date": selected_date, "data": data}


async def stats(
    session: AsyncSession, user_id: str, start: date, end: date, day: AppDay
) -> dict[str, Any]:
    """기록 통계. summary는 삭제된 루틴을 포함한 전 기간, routines는 삭제 안 된 루틴을 목록 순서로."""
    if start > end or (end - start).days >= STATS_MAX_DAYS:
        raise errors.AppError(
            "VALIDATION", 422, f"조회 기간은 from ≤ to이고 {STATS_MAX_DAYS}일 이하여야 해요.",
        )
    profile = await _load_profile(session, user_id)
    today = day.local_date
    rows = await _all_routines(session, profile.id)
    records = await _routine_records(session, profile, rows, today)
    totals = routine_records.totals(records.values(), today)
    window = list(routine_records.dates(start, min(end, today)))
    items = []
    for row in rows:
        if row.deleted_at is not None:
            continue
        record = records[row.id]
        instances = record.instance_dates(today)
        items.append({
            "id": str(row.id),
            "name": i18n.localized_name(
                row.name_i18n, profile.language, row.name, kind="routine", key=str(row.id),
            ),
            "icon": row.icon,
            "color": row.color,
            "scheduled_dates": [d for d in window if d in instances],
            "completed_dates": [d for d in window if d in record.completed],
            "skipped_dates": [d for d in window if record.skipped(d)],
            **routine_records.routine_summary(record, today),
        })
    return {
        "today": today,
        "summary": routine_records.summary(totals, today),
        "days": [
            {"date": d, "scheduled": scheduled, "completed": completed}
            for d, scheduled, completed in map(totals.day, window)
        ],
        "routines": items,
    }


async def _reschedule(
    session: AsyncSession, user_id: str, r: Routine, today: date, days: list[int]
) -> None:
    """오늘부터 새 요일을 적용한다. 이력이 없는 루틴은 기존 요일을 생성일부터의 이력으로 먼저 남긴다."""
    zone = safe_zone((await _load_profile(session, user_id)).timezone)
    stored = (await session.execute(
        select(RoutineSchedule.effective_from, RoutineSchedule.days_of_week)
        .where(RoutineSchedule.routine_id == r.id)
    )).all()
    versions = routine_records.schedule(
        stored, r.days_of_week, r.created_at.astimezone(zone).date(), _local_date(r.updated_at, zone),
    )
    await session.execute(delete(RoutineSchedule).where(
        RoutineSchedule.routine_id == r.id, RoutineSchedule.effective_from >= today,
    ))
    stmt = pg_insert(RoutineSchedule).values([
        {"routine_id": r.id, "user_id": r.user_id, "effective_from": start, "days_of_week": list(values)}
        for start, values in routine_records.reschedule(versions, today, days)
    ])
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["routine_id", "effective_from"],
        set_={"days_of_week": stmt.excluded.days_of_week},
    ))


async def update_routine(
    session: AsyncSession, user_id: str, routine_id: str, req, day: AppDay
) -> None:
    uid = _uid(user_id)
    r = await _load_owned(session, uid, routine_id)
    if req.name is not None:
        # 클라는 로컬라이즈된 표시 이름을 그대로 되돌려 보낸다 — 원본·번역값과 같으면 rename 아님.
        i18n_values = (
            {v for v in r.name_i18n.values() if isinstance(v, str)}
            if isinstance(r.name_i18n, dict) else set()
        )
        if req.name != r.name and req.name not in i18n_values:
            r.name = req.name
            r.name_i18n = None  # 유저가 이름을 직접 바꾸면 기본 다국어는 무효(SOMA-346)
    if req.reminder_enabled is not None:
        r.reminder_enabled = req.reminder_enabled
    if "reminder_time" in req.model_fields_set:  # null 명시=제거, 생략=변경 없음
        r.reminder_time = req.reminder_time
    if req.icon is not None:
        r.icon = req.icon
    if req.color is not None:
        r.color = req.color
    # 생략=변경 없음(빈 배열은 스키마에서 422)
    if req.days_of_week is not None and req.days_of_week != r.days_of_week:
        await _reschedule(session, user_id, r, day.local_date, req.days_of_week)
        r.days_of_week = req.days_of_week
        r.frequency_per_week = len(req.days_of_week)
    await session.commit()


async def delete_routine(session: AsyncSession, user_id: str, routine_id: str, day: AppDay) -> None:
    uid = _uid(user_id)
    r = await _load_owned(session, uid, routine_id)
    r.deleted_at = datetime.now(timezone.utc)  # soft delete(통계 보존)
    r.deleted_on = day.local_date
    await session.commit()


async def complete(session: AsyncSession, user_id: str, routine_id: str, day: AppDay | None = None) -> dict[str, Any]:
    uid, ad = await _today(session, user_id, day)
    r = await _load_owned(session, uid, routine_id)
    stmt = pg_insert(RoutineCompletion).values(routine_id=r.id, user_id=uid, activity_date=ad)
    stmt = stmt.on_conflict_do_nothing(index_elements=["routine_id", "activity_date"])
    await session.execute(stmt)
    await session.execute(
        delete(RoutineSkip).where(RoutineSkip.routine_id == r.id, RoutineSkip.activity_date == ad)
    )
    await session.commit()
    count = (
        await session.execute(
            select(func.count())
            .select_from(RoutineCompletion)
            .where(RoutineCompletion.user_id == uid, RoutineCompletion.activity_date == ad)
        )
    ).scalar() or 0
    return {"completed_today": True, "completed_count_today": count}


async def uncomplete(session: AsyncSession, user_id: str, routine_id: str, day: AppDay | None = None) -> None:
    uid, ad = await _today(session, user_id, day)
    r = await _load_owned(session, uid, routine_id)
    await session.execute(
        delete(RoutineCompletion).where(
            RoutineCompletion.routine_id == r.id, RoutineCompletion.activity_date == ad
        )
    )
    await session.execute(
        delete(RoutineSkip).where(RoutineSkip.routine_id == r.id, RoutineSkip.activity_date == ad)
    )
    await session.commit()


async def skip(session: AsyncSession, user_id: str, routine_id: str, day: AppDay) -> None:
    uid, ad = _uid(user_id), day.local_date
    r = await _load_owned(session, uid, routine_id)
    done = (await session.execute(
        select(RoutineCompletion.id).where(
            RoutineCompletion.routine_id == r.id, RoutineCompletion.activity_date == ad,
        )
    )).first()
    if done is not None:
        raise errors.AppError("ROUTINE_ALREADY_COMPLETED", 409, "완료한 루틴은 건너뛸 수 없어요.")
    stmt = pg_insert(RoutineSkip).values(routine_id=r.id, user_id=uid, activity_date=ad)
    await session.execute(stmt.on_conflict_do_nothing(index_elements=["routine_id", "activity_date"]))
    await session.commit()


async def unskip(session: AsyncSession, user_id: str, routine_id: str, day: AppDay) -> None:
    uid = _uid(user_id)
    r = await _load_owned(session, uid, routine_id)
    await session.execute(
        delete(RoutineSkip).where(RoutineSkip.routine_id == r.id, RoutineSkip.activity_date == day.local_date)
    )
    await session.commit()


async def statistics(session: AsyncSession, user_id: str, routine_id: str, day: AppDay | None = None) -> dict[str, Any]:
    uid, ad = await _today(session, user_id, day)
    r = await _load_owned(session, uid, routine_id)
    dates = (
        (
            await session.execute(
                select(RoutineCompletion.activity_date).where(RoutineCompletion.routine_id == r.id)
            )
        ).scalars().all()
    )
    date_set = set(dates)
    streak = _streak(ad, date_set)
    # 이번 주(월~일): 요일별 완료 여부 + 수행 횟수
    wk_start, wk_end = _week_bounds(ad)
    by_weekday = {str(i): False for i in range(1, 8)}
    week_count = 0
    last_30_dates = []
    recent = 0
    for d in dates:
        if wk_start <= d <= wk_end:
            by_weekday[str(d.isoweekday())] = True
            week_count += 1
        age = (ad - d).days
        # Preserve the historical one-sided boundary, including dates ahead of ad.
        # Only the returned window needs sorting; streak uses the complete date set.
        if age < 30:
            last_30_dates.append(d)
            if age < 28:
                recent += 1
    last_30 = [d.isoformat() for d in sorted(last_30_dates)]
    # 완료율: 최근 4주 완료수 / (목표 × 4), 상한 1.0
    target = max(1, len(r.days_of_week) * 4)
    return {
        "streak": streak,
        "completed_today": ad in date_set,   # 완료 여부 = 오늘
        "target_count": len(r.days_of_week),  # 하위호환 필드 — 항상 요일 수
        "days_of_week": r.days_of_week,
        "this_week": {
            "completed_count": week_count,     # 이번 주 수행 횟수
            "by_weekday": by_weekday,          # 이번 주 요일별 완료 여부(월~일)
        },
        "last_30_days": last_30,
        "completion_rate": round(min(1.0, recent / target), 2),
    }


async def _active_templates(session: AsyncSession) -> list[tuple[RoutineTemplateCategory, RoutineTemplate]]:
    return [tuple(row) for row in (await session.execute(
        select(RoutineTemplateCategory, RoutineTemplate)
        .join(RoutineTemplate, RoutineTemplate.category_id == RoutineTemplateCategory.id)
        .where(RoutineTemplateCategory.is_active.is_(True), RoutineTemplate.is_active.is_(True))
        .order_by(
            RoutineTemplateCategory.sort_order, RoutineTemplateCategory.id,
            RoutineTemplate.sort_order, RoutineTemplate.id,
        )
    )).all()]


async def _added_template_ids(session: AsyncSession, uid: uuid.UUID) -> set[str]:
    return set((await session.execute(
        select(Routine.template_id).where(
            Routine.user_id == uid, Routine.deleted_at.is_(None), Routine.template_id.is_not(None),
        )
    )).scalars().all())


def _template_name(name_i18n: dict, language: str | None) -> str:
    return name_i18n.get(i18n.resolve(language)) or name_i18n["ko"]


async def routine_templates(session: AsyncSession, user_id: str) -> dict[str, Any]:
    profile = await _load_profile(session, user_id)
    added = await _added_template_ids(session, profile.id)
    categories: dict[str, dict[str, Any]] = {}
    for category, template in await _active_templates(session):
        entry = categories.setdefault(category.id, {
            "id": category.id,
            "name": _template_name(category.name_i18n, profile.language),
            "templates": [],
        })
        entry["templates"].append({
            "id": template.id,
            "name": _template_name(template.name_i18n, profile.language),
            "icon": template.icon,
            "color": template.color,
            "days_of_week": sorted(set(template.days_of_week)),
            "recommended": template.is_recommended,
            "added": template.id in added,
        })
    return {
        "selection_completed": profile.routine_template_selection_at is not None,
        "categories": list(categories.values()),
    }


async def select_templates(session: AsyncSession, user_id: str, req, day: AppDay) -> dict[str, Any]:
    """고른 템플릿으로 루틴을 만든다. 이미 추가된 템플릿은 건너뛰고 이번에 만든 루틴만 돌려준다."""
    profile = await _load_profile(session, user_id)
    await advisory_xact_lock(session, profile.id)
    templates = [template for _, template in await _active_templates(session)]
    unknown = set(req.template_ids) - {template.id for template in templates}
    if unknown:
        raise errors.AppError(
            "VALIDATION", 422, "추가할 수 없는 템플릿이에요.", {"template_ids": sorted(unknown)},
        )
    added = await _added_template_ids(session, profile.id)
    now = datetime.now(timezone.utc)
    rows: list[Routine] = []
    for template in templates:
        if template.id not in req.template_ids or template.id in added:
            continue
        days = sorted(set(template.days_of_week))
        rows.append(Routine(
            id=uuid.uuid4(), user_id=profile.id,
            name=_template_name(template.name_i18n, profile.language),
            name_i18n=dict(template.name_i18n),
            icon=template.icon, color=template.color, template_id=template.id,
            frequency_per_week=len(days), days_of_week=days,
            reminder_enabled=False, reminder_time=None,
            created_at=now + timedelta(microseconds=len(rows)),
        ))
    session.add_all(rows)
    await session.flush()
    session.add_all([
        RoutineSchedule(
            routine_id=r.id, user_id=r.user_id, effective_from=day.local_date, days_of_week=r.days_of_week,
        )
        for r in rows
    ])
    if profile.routine_template_selection_at is None:
        profile.routine_template_selection_at = now
    await session.commit()
    return {"data": [_dto(r, False, profile.language) for r in rows]}
