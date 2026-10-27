"""오늘의 글귀 — 날짜별로 저장한 문장 중 현지 날짜 이하의 가장 최근 문장.

운영자가 `daily_affirmations`에 날짜마다 문장을 넣는다. 오늘 문장이 없으면 가장 최근 문장을
계속 내려준다. 사용자별 확인 표시는 앱이 기기에 저장하므로 서버는 기록하지 않는다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_day import AppDay, validate_app_timezone
from app.core.errors import AppError
from app.models.daily_affirmation import DailyAffirmation
from app.services import privacy
from app.services.account import _load_profile, _uid
from app.services.i18n import resolve


async def _day(
    session: AsyncSession, user_id: str, timezone_name: str | None, now: datetime
) -> AppDay:
    validate_app_timezone(timezone_name)
    if timezone_name is None:
        timezone_name = (await _load_profile(session, user_id)).timezone
    return AppDay.at(now, timezone_name)


async def _latest(session: AsyncSession, local_date: date) -> DailyAffirmation | None:
    return (
        await session.execute(
            select(DailyAffirmation)
            .where(DailyAffirmation.local_date <= local_date)
            .order_by(DailyAffirmation.local_date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


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
    day = await _day(session, user_id, timezone_name, now_utc or datetime.now(timezone.utc))
    row = await _latest(session, day.local_date)
    if row is None:
        raise AppError("DAILY_AFFIRMATION_NOT_FOUND", 404, "아직 오늘의 글귀가 없어요.")
    language = resolve(locale)
    return {
        "local_date": day.local_date,
        "locale": language,
        "text": getattr(row, f"text_{language}"),
    }
