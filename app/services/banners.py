"""Authenticated, request-scoped banner selection and data binding."""

from datetime import datetime

from sqlalchemy import exists, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_day import AppDay, validate_app_timezone
from app.models.routine import Routine, RoutineCompletion
from app.services.account import _load_profile, _uid
from app.services.banner_catalog import BannerCatalog, render_feed, select_candidates
from app.services.i18n import resolve


async def remaining_today(session: AsyncSession, user_id: str, day: AppDay) -> int:
    uid = _uid(user_id)
    completed = exists(
        select(RoutineCompletion.id).where(
            RoutineCompletion.user_id == uid,
            RoutineCompletion.routine_id == Routine.id,
            RoutineCompletion.activity_date == day.local_date,
        )
    )
    query = (
        select(func.count())
        .select_from(Routine)
        .where(
            Routine.user_id == uid,
            Routine.deleted_at.is_(None),
            Routine.days_of_week.any(day.local_date.isoweekday()),
            ~completed,
        )
    )
    async with session.begin_nested():
        return (await session.execute(query)).scalar_one()


async def list_banners(
    catalog: BannerCatalog,
    session: AsyncSession,
    user_id: str,
    *,
    now: datetime,
    platform: str,
    app_version: str,
    locale: str | None,
    timezone_name: str | None,
    capabilities: frozenset[str],
):
    validate_app_timezone(timezone_name)
    candidates = select_candidates(
        catalog,
        now=now,
        platform=platform,
        app_version=app_version,
        locale=resolve(locale),
        supported=capabilities,
    )
    needs_day = any(banner.bindings for banner, _, _ in candidates)
    needs_count = any(
        binding.source == "routines.remaining_today"
        for banner, _, _ in candidates
        for binding in banner.bindings.values()
    )
    day = None
    if needs_day:
        if timezone_name is None:
            profile = await _load_profile(session, user_id)
            timezone_name = profile.timezone
        day = AppDay.at(now, timezone_name)
    remaining = None
    if needs_count:
        try:
            remaining = await remaining_today(session, user_id, day)
        except DBAPIError as exc:
            # begin_nested has restored the transaction; a lost connection cannot be isolated.
            if exc.connection_invalidated:
                raise
    return render_feed(
        catalog,
        candidates,
        now=now,
        local_date=day.local_date if day else None,
        day_ends_at=day.ends_at if day else None,
        remaining=remaining,
    )
