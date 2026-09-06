"""Opt-in isolated PostgreSQL tests; never use the configured application database.

BANNER_TEST_DATABASE_URL=postgresql+asyncpg://...@127.0.0.1:55437/postgres pytest ...
Each test creates and drops only its own random schema.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.app_day import AppDay
from app.core.db import Base
from app.models.routine import Routine, RoutineCompletion
from app.services.banners import remaining_today
from app.services import routine


@pytest.fixture
async def session():
    dsn = os.environ.get("BANNER_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("BANNER_TEST_DATABASE_URL is required for isolated local PostgreSQL")
    if urlsplit(dsn).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("banner integration tests accept a local database only")
    schema = "banner_test_" + uuid.uuid4().hex
    engine = create_async_engine(dsn, execution_options={"schema_translate_map": {None: schema}})
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[Routine.__table__, RoutineCompletion.__table__]
            )
        )
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


def new_routine(uid, day, **changes):
    return Routine(
        id=uuid.uuid4(),
        user_id=uid,
        name="Test",
        frequency_per_week=1,
        days_of_week=changes.pop("days_of_week", [day.local_date.isoweekday()]),
        reminder_enabled=False,
        **changes,
    )


async def test_count_filters_owner_weekday_deleted_and_today_completion(session):
    day = AppDay.at(datetime(2026, 9, 6, 15, tzinfo=timezone.utc), "Asia/Seoul")
    uid = uuid.uuid4()
    pending = new_routine(uid, day)
    completed = new_routine(uid, day)
    yesterday = new_routine(uid, day)
    rows = [
        pending,
        completed,
        yesterday,
        new_routine(uid, day, days_of_week=[2]),
        new_routine(uid, day, deleted_at=day.served_at),
        new_routine(uuid.uuid4(), day),
    ]
    session.add_all(rows)
    await session.flush()
    session.add_all(
        [
            RoutineCompletion(routine_id=completed.id, user_id=uid, activity_date=day.local_date),
            RoutineCompletion(
                routine_id=yesterday.id,
                user_id=uid,
                activity_date=day.local_date - timedelta(days=1),
            ),
        ]
    )
    await session.commit()
    assert await remaining_today(session, str(uid), day) == 2
    # The same UTC instant is still Sunday in Los Angeles, where none are scheduled.
    other_day = AppDay.at(day.served_at, "America/Los_Angeles")
    assert await remaining_today(session, str(uid), other_day) == 0


async def test_completion_uses_requested_day_and_is_idempotent(session):
    day = AppDay.at(datetime(2026, 9, 6, 15, tzinfo=timezone.utc), "Asia/Seoul")
    uid = uuid.uuid4()
    row = new_routine(uid, day)
    session.add(row)
    await session.commit()
    assert await remaining_today(session, str(uid), day) == 1
    await routine.complete(session, str(uid), str(row.id), day)
    await routine.complete(session, str(uid), str(row.id), day)
    assert await remaining_today(session, str(uid), day) == 0
    result = await routine.statistics(session, str(uid), str(row.id), day)
    assert result["streak"] == 1
    await routine.uncomplete(session, str(uid), str(row.id), day)
    assert await remaining_today(session, str(uid), day) == 1


async def test_count_sql_failure_restores_transaction(session):
    from sqlalchemy.exc import DBAPIError

    day = AppDay.at(datetime(2026, 9, 6, 15, tzinfo=timezone.utc), "Asia/Seoul")
    schema = session.bind.get_execution_options()["schema_translate_map"][None]
    await session.execute(text(f'ALTER TABLE "{schema}".routines DROP COLUMN days_of_week'))
    await session.commit()
    with pytest.raises(DBAPIError):
        await remaining_today(session, str(uuid.uuid4()), day)
    assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
