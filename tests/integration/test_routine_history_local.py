"""Opt-in real SQL history coverage; random schema on localhost only.

ROUTINE_TEST_DATABASE_URL=postgresql+asyncpg://...@127.0.0.1:55438/postgres
ROUTINE_HISTORY_FIXTURE_OUT optionally writes the actual API response for Dart contract tests.
"""
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api import routine as routine_api
from app.core.app_day import AppDay
from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.main import app
from app.models.routine import Routine, RoutineCompletion
from app.services import routine

UID = uuid.UUID('00000000-0000-0000-0000-000000000001')
SELECTED = date(2025, 12, 31)
DAY = AppDay.at(datetime(2026, 9, 13, 15, tzinfo=timezone.utc), 'Asia/Seoul')


@pytest.fixture
async def session(monkeypatch):
    dsn = os.environ.get('ROUTINE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('ROUTINE_TEST_DATABASE_URL is required for isolated local PostgreSQL')
    if urlsplit(dsn).hostname not in {'localhost', '127.0.0.1', '::1'}:
        pytest.fail('history integration tests accept a local database only')
    schema = 'routine_history_test_' + uuid.uuid4().hex
    engine = create_async_engine(dsn, execution_options={'schema_translate_map': {None: schema}})
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(lambda sync: Base.metadata.create_all(
            sync, tables=[Routine.__table__, RoutineCompletion.__table__],
        ))

    async def profile(session, user_id):
        return SimpleNamespace(id=uuid.UUID(user_id), timezone='Asia/Seoul', language='ko')

    monkeypatch.setattr(routine, '_load_profile', profile)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


def new_routine(index, **changes):
    days = changes.pop('days_of_week', [3])
    return Routine(
        id=uuid.UUID(int=index), user_id=changes.pop('user_id', UID),
        name=changes.pop('name', f'루틴 {index}'),
        days_of_week=days, frequency_per_week=len(days),
        created_at=changes.pop('created_at', datetime(2025, 1, 1, tzinfo=timezone.utc)),
        reminder_enabled=changes.pop('reminder_enabled', False), **changes,
    )


async def test_history_filters_statistics_readonly_and_actual_api_contract(session):
    rows = [
        new_routine(11, name='이름을 바꾼 루틴', days_of_week=[1], reminder_enabled=True, reminder_time=time(7, 30)),
        new_routine(12, name='미완료 루틴'),
        new_routine(13, days_of_week=[4]),
        new_routine(14, deleted_at=DAY.served_at),
        new_routine(15, user_id=uuid.UUID(int=2)),
        new_routine(16, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        new_routine(17, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ]
    session.add_all(rows)
    await session.flush()
    for row, dates in [(rows[0], [SELECTED-timedelta(days=n) for n in range(3)] + [SELECTED+timedelta(days=1), DAY.local_date]), (rows[3], [SELECTED]), (rows[4], [SELECTED]), (rows[6], [SELECTED])]:
        session.add_all([RoutineCompletion(routine_id=row.id, user_id=row.user_id, activity_date=d) for d in dates])
    await session.commit()
    before = (await session.execute(select(RoutineCompletion.id, RoutineCompletion.activity_date))).all()

    async def get_test_session():
        yield session

    from fastapi import Response

    async def request_day(response: Response):
        response.headers.update(DAY.headers())
        return DAY

    app.dependency_overrides[get_session] = get_test_session
    app.dependency_overrides[get_current_user] = lambda: str(UID)
    app.dependency_overrides[routine_api.request_day] = request_day
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://local') as client:
            response = await client.get('/routines/history', params={'date': SELECTED.isoformat()}, headers={'X-App-Timezone':'Asia/Seoul'})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['date'] == '2025-12-31'
    assert response.headers['x-app-local-date'] == '2026-09-14'
    assert [r['id'] for r in body['data']] == [str(rows[i].id) for i in (0, 1, 6)]
    completed, pending, _ = body['data']
    assert completed['completed'] and completed['streak'] == 3
    assert completed['name'] == '이름을 바꾼 루틴'
    assert completed['reminder_time'] == '07:30'
    assert completed['this_week'] == {'completed_count': 3, 'by_weekday': {str(i): i <= 3 for i in range(1, 8)}}
    assert not pending['completed'] and pending['streak'] == 0
    assert (await session.execute(select(RoutineCompletion.id, RoutineCompletion.activity_date))).all() == before
    assert not session.new and not session.dirty and not session.deleted
    if output := os.environ.get('ROUTINE_HISTORY_FIXTURE_OUT'):
        Path(output).write_text(response.text + '\n')


async def test_created_date_uses_request_timezone_and_null_is_unknown(session):
    row = new_routine(21, created_at=datetime(2025, 12, 31, 15, tzinfo=timezone.utc))
    unknown = new_routine(22)
    session.add_all([row, unknown])
    await session.commit()
    # Explicit SQL preserves the nullable legacy timestamp instead of its INSERT default.
    unknown.created_at = None
    await session.commit()
    seoul = await routine.history(session, str(UID), SELECTED, DAY)
    la = await routine.history(session, str(UID), SELECTED, DAY, 'America/Los_Angeles')
    assert [r['id'] for r in seoul['data']] == [str(unknown.id)]
    assert [r['id'] for r in la['data']] == [str(row.id), str(unknown.id)]


async def test_deleted_routine_is_absent_after_requery_and_owner_isolated(session):
    row = new_routine(31)
    session.add(row)
    await session.commit()
    assert len((await routine.history(session, str(UID), SELECTED, DAY))['data']) == 1
    assert (await routine.history(session, str(uuid.UUID(int=2)), SELECTED, DAY))['data'] == []
    await routine.delete_routine(session, str(UID), str(row.id))
    assert (await routine.history(session, str(UID), SELECTED, DAY))['data'] == []
