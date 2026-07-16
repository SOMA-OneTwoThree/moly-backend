"""루틴 — CRUD(soft delete)·완료 체크·통계. 알림은 클라 로컬(서버는 스케줄 데이터만)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.time_utils import current_reward_date
from app.models.routine import Routine, RoutineCompletion
from app.services.account import _load_profile, _uid


def _dto(r: Routine, completed_today: bool) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "name": r.name,
        "frequency_per_week": len(r.days_of_week),  # 하위호환 필드 — 항상 요일 수
        "days_of_week": r.days_of_week,
        "reminder_enabled": r.reminder_enabled,
        "reminder_time": r.reminder_time.strftime("%H:%M") if r.reminder_time else None,
        "completed_today": completed_today,
    }


def _week_bounds(ad):
    """ad(로컬 activity_date)가 속한 주의 월~일 경계(ISO, 월요일 시작)."""
    monday = ad - timedelta(days=ad.isoweekday() - 1)
    return monday, monday + timedelta(days=6)


async def _today(session: AsyncSession, user_id: str):
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


async def list_routines(session: AsyncSession, user_id: str) -> dict[str, Any]:
    uid, ad = await _today(session, user_id)
    rows = list(
        (
            await session.execute(
                select(Routine)
                .where(Routine.user_id == uid, Routine.deleted_at.is_(None))
                .order_by(Routine.created_at)
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
    return {"data": [_dto(r, r.id in done) for r in rows]}


async def create_routine(session: AsyncSession, user_id: str, req) -> dict[str, Any]:
    uid = _uid(user_id)
    r = Routine(
        user_id=uid, name=req.name,
        frequency_per_week=len(req.days_of_week), days_of_week=req.days_of_week,
        reminder_enabled=req.reminder_enabled, reminder_time=req.reminder_time,
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return _dto(r, completed_today=False)


async def update_routine(session: AsyncSession, user_id: str, routine_id: str, req) -> None:
    uid = _uid(user_id)
    r = await _load_owned(session, uid, routine_id)
    if req.name is not None:
        r.name = req.name
    if req.reminder_enabled is not None:
        r.reminder_enabled = req.reminder_enabled
    if req.reminder_time is not None:
        r.reminder_time = req.reminder_time
    if req.days_of_week is not None:  # 생략=변경 없음(빈 배열은 스키마에서 422)
        r.days_of_week = req.days_of_week
        r.frequency_per_week = len(req.days_of_week)
    await session.commit()


async def delete_routine(session: AsyncSession, user_id: str, routine_id: str) -> None:
    uid = _uid(user_id)
    r = await _load_owned(session, uid, routine_id)
    r.deleted_at = datetime.now(timezone.utc)  # soft delete(통계 보존)
    await session.commit()


async def complete(session: AsyncSession, user_id: str, routine_id: str) -> dict[str, Any]:
    uid, ad = await _today(session, user_id)
    r = await _load_owned(session, uid, routine_id)
    stmt = pg_insert(RoutineCompletion).values(routine_id=r.id, user_id=uid, activity_date=ad)
    stmt = stmt.on_conflict_do_nothing(index_elements=["routine_id", "activity_date"])
    await session.execute(stmt)
    await session.commit()
    count = (
        await session.execute(
            select(func.count())
            .select_from(RoutineCompletion)
            .where(RoutineCompletion.user_id == uid, RoutineCompletion.activity_date == ad)
        )
    ).scalar() or 0
    return {"completed_today": True, "completed_count_today": count}


async def uncomplete(session: AsyncSession, user_id: str, routine_id: str) -> None:
    uid, ad = await _today(session, user_id)
    r = await _load_owned(session, uid, routine_id)
    from sqlalchemy import delete

    await session.execute(
        delete(RoutineCompletion).where(
            RoutineCompletion.routine_id == r.id, RoutineCompletion.activity_date == ad
        )
    )
    await session.commit()


async def statistics(session: AsyncSession, user_id: str, routine_id: str) -> dict[str, Any]:
    uid, ad = await _today(session, user_id)
    r = await _load_owned(session, uid, routine_id)
    dates = sorted(
        (
            await session.execute(
                select(RoutineCompletion.activity_date).where(RoutineCompletion.routine_id == r.id)
            )
        ).scalars().all()
    )
    date_set = set(dates)
    # streak: 오늘부터 뒤로 연속 완료 일수(지정 요일 무관, 단순 달력일 연속)
    streak = 0
    cursor = ad
    while cursor in date_set:
        streak += 1
        cursor = cursor - timedelta(days=1)
    # 이번 주(월~일): 요일별 완료 여부 + 수행 횟수
    wk_start, wk_end = _week_bounds(ad)
    by_weekday = {str(i): False for i in range(1, 8)}
    week_count = 0
    for d in dates:
        if wk_start <= d <= wk_end:
            by_weekday[str(d.isoweekday())] = True
            week_count += 1
    last_30 = [d.isoformat() for d in dates if (ad - d).days < 30]
    # 완료율: 최근 4주 완료수 / (목표 × 4), 상한 1.0
    recent = sum(1 for d in dates if (ad - d).days < 28)
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
