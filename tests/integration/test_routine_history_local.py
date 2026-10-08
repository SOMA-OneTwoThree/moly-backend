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
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api import routine as routine_api
from app.core.app_day import AppDay
from app.core.db import Base, get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.models.routine import (
    Routine,
    RoutineCompletion,
    RoutineSchedule,
    RoutineSkip,
    RoutineTemplate,
    RoutineTemplateCategory,
)
from app.schemas.routine import CreateRoutineRequest, PatchRoutineRequest, RoutineTemplateSelectionRequest
from app.services import routine

UID = uuid.UUID('00000000-0000-0000-0000-000000000001')
SELECTED = date(2025, 12, 31)
DAY = AppDay.at(datetime(2026, 9, 13, 15, tzinfo=timezone.utc), 'Asia/Seoul')
TABLES = [Routine, RoutineCompletion, RoutineSchedule, RoutineSkip, RoutineTemplateCategory, RoutineTemplate]
PROFILES: dict[uuid.UUID, SimpleNamespace] = {}


def seoul(year, month, day_of_month, hour=12):
    return AppDay.at(datetime(year, month, day_of_month, hour, tzinfo=timezone.utc) - timedelta(hours=9),
                     'Asia/Seoul')


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
            sync, tables=[model.__table__ for model in TABLES],
        ))
    PROFILES.clear()

    async def profile(session, user_id):
        uid = uuid.UUID(user_id)
        return PROFILES.setdefault(uid, SimpleNamespace(
            id=uid, timezone='Asia/Seoul', language='ko', routine_template_selection_at=None,
        ))

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


async def schedules(session, routine_id):
    rows = (await session.execute(
        select(RoutineSchedule.effective_from, RoutineSchedule.days_of_week)
        .where(RoutineSchedule.routine_id == routine_id).order_by(RoutineSchedule.effective_from)
    )).all()
    return [(start, list(days)) for start, days in rows]


async def test_history_filters_statistics_readonly_and_actual_api_contract(session):
    rows = [
        new_routine(11, name='이름을 바꾼 루틴', days_of_week=[1], reminder_enabled=True, reminder_time=time(7, 30)),
        new_routine(12, name='미완료 루틴', icon='memo', color='lavender'),
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
    assert [r['id'] for r in body['data']] == [str(rows[i].id) for i in (0, 1, 3, 6)]
    completed, pending, deleted_later, _ = body['data']
    assert completed['completed'] and completed['streak'] == 3 and not completed['skipped']
    assert completed['name'] == '이름을 바꾼 루틴'
    assert completed['reminder_time'] == '07:30'
    assert completed['days_of_week'] == [1]
    assert completed['this_week'] == {'completed_count': 3, 'by_weekday': {str(i): i <= 3 for i in range(1, 8)}}
    assert not pending['completed'] and pending['streak'] == 0
    assert (pending['icon'], pending['color']) == ('memo', 'lavender')
    assert deleted_later['completed'] and (deleted_later['icon'], deleted_later['color']) == ('seedling', 'peach')
    assert (await session.execute(select(RoutineCompletion.id, RoutineCompletion.activity_date))).all() == before
    assert not session.new and not session.dirty and not session.deleted
    if output := os.environ.get('ROUTINE_HISTORY_FIXTURE_OUT'):
        Path(output).write_text(response.text + '\n')


async def test_created_date_without_history_uses_profile_time_zone(session):
    row = new_routine(21, created_at=datetime(2025, 12, 31, 15, tzinfo=timezone.utc))
    session.add(row)
    await session.commit()
    assert (await routine.history(session, str(UID), SELECTED, DAY))['data'] == []
    PROFILES[UID].timezone = 'America/Los_Angeles'
    assert [r['id'] for r in (await routine.history(session, str(UID), SELECTED, DAY))['data']] == [str(row.id)]


async def test_deleted_routine_keeps_its_past_and_owner_isolated(session):
    row = new_routine(31)
    session.add(row)
    await session.commit()
    assert len((await routine.history(session, str(UID), SELECTED, DAY))['data']) == 1
    assert (await routine.history(session, str(uuid.UUID(int=2)), SELECTED, DAY))['data'] == []
    deleted_on = seoul(2026, 9, 16)
    await routine.delete_routine(session, str(UID), str(row.id), deleted_on)
    assert row.deleted_on == date(2026, 9, 16)
    assert len((await routine.history(session, str(UID), SELECTED, deleted_on))['data']) == 1
    assert (await routine.history(session, str(UID), date(2026, 9, 16), deleted_on))['data'] == []
    assert (await routine.list_routines(session, str(UID), deleted_on))['data'] == []


async def test_days_change_applies_from_that_day_and_keeps_earlier_days(session):
    created = await routine.create_routine(
        session, str(UID),
        CreateRoutineRequest(name='산책', days_of_week=[3, 1], icon='person_walking', color='green'),
        seoul(2026, 9, 7),
    )
    rid = uuid.UUID(created['id'])
    assert (created['icon'], created['color'], created['template_id']) == ('person_walking', 'green', None)
    assert await schedules(session, rid) == [(date(2026, 9, 7), [1, 3])]
    thursday = seoul(2026, 9, 10)
    await routine.update_routine(session, str(UID), str(rid), PatchRoutineRequest(days_of_week=[4]), thursday)
    await routine.update_routine(session, str(UID), str(rid), PatchRoutineRequest(days_of_week=[5]), thursday)
    assert await schedules(session, rid) == [(date(2026, 9, 7), [1, 3]), (date(2026, 9, 10), [5])]
    wednesday = (await routine.history(session, str(UID), date(2026, 9, 9), thursday))['data']
    assert [r['days_of_week'] for r in wednesday] == [[1, 3]]
    assert (await routine.history(session, str(UID), date(2026, 9, 10), thursday))['data'] == []
    friday = seoul(2026, 9, 11)
    assert [r['days_of_week'] for r in (await routine.history(session, str(UID), date(2026, 9, 11), friday))['data']] == [[5]]
    await routine.delete_routine(session, str(UID), str(rid), friday)
    assert (await routine.history(session, str(UID), date(2026, 9, 11), friday))['data'] == []


async def test_first_days_change_of_a_routine_without_history_preserves_its_past(session):
    row = new_routine(41, days_of_week=[1, 2, 3, 4, 5, 6, 7], created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    session.add(row)
    await session.commit()
    today = seoul(2026, 9, 14)
    await routine.update_routine(session, str(UID), str(row.id), PatchRoutineRequest(days_of_week=[1]), today)
    assert await schedules(session, row.id) == [(date(2026, 9, 1), [1, 2, 3, 4, 5, 6, 7]), (date(2026, 9, 14), [1])]
    assert len((await routine.history(session, str(UID), date(2026, 9, 10), today))['data']) == 1


async def test_skip_complete_and_stats_follow_today(session):
    today = seoul(2026, 9, 17)  # 목요일
    created = await routine.create_routine(
        session, str(UID), CreateRoutineRequest(name='물', days_of_week=[1, 2, 3, 4, 5, 6, 7]), seoul(2026, 9, 14),
    )
    rid = created['id']
    for day in (seoul(2026, 9, 14), seoul(2026, 9, 15)):
        await routine.complete(session, str(UID), rid, day)
    await routine.skip(session, str(UID), rid, seoul(2026, 9, 16))
    await routine.skip(session, str(UID), rid, today)
    await routine.skip(session, str(UID), rid, today)
    listed = (await routine.list_routines(session, str(UID), today))['data'][0]
    assert (listed['completed_today'], listed['skipped_today']) == (False, True)
    stats = await routine.stats(session, str(UID), date(2026, 9, 14), date(2026, 9, 30), today)
    assert [d['scheduled'] for d in stats['days']] == [1, 1, 0, 0]
    assert stats['summary']['current_streak'] == 2
    item = stats['routines'][0]
    assert item['skipped_dates'] == [date(2026, 9, 16), date(2026, 9, 17)]
    assert item['scheduled_dates'] == [date(2026, 9, 14), date(2026, 9, 15)]
    history = (await routine.history(session, str(UID), date(2026, 9, 16), today))['data']
    assert [(r['completed'], r['skipped']) for r in history] == [(False, True)]
    await routine.complete(session, str(UID), rid, today)
    assert await session.scalar(select(RoutineSkip.routine_id).where(RoutineSkip.activity_date == today.local_date)) is None
    listed = (await routine.list_routines(session, str(UID), today))['data'][0]
    assert (listed['completed_today'], listed['skipped_today']) == (True, False)
    with pytest.raises(AppError) as error:
        await routine.skip(session, str(UID), rid, today)
    assert error.value.http_status == 409
    await routine.uncomplete(session, str(UID), rid, today)
    await routine.skip(session, str(UID), rid, today)
    await routine.unskip(session, str(UID), rid, today)
    await routine.unskip(session, str(UID), rid, today)
    listed = (await routine.list_routines(session, str(UID), today))['data'][0]
    assert (listed['completed_today'], listed['skipped_today']) == (False, False)
    with pytest.raises(AppError):
        await routine.skip(session, str(uuid.UUID(int=2)), rid, today)
    session.add_all([
        RoutineCompletion(routine_id=uuid.UUID(rid), user_id=UID, activity_date=today.local_date),
        RoutineSkip(routine_id=uuid.UUID(rid), user_id=UID, activity_date=today.local_date),
    ])
    await session.commit()
    listed = (await routine.list_routines(session, str(UID), today))['data'][0]
    assert (listed['completed_today'], listed['skipped_today']) == (True, False)
    await routine.uncomplete(session, str(UID), rid, today)
    listed = (await routine.list_routines(session, str(UID), today))['data'][0]
    assert (listed['completed_today'], listed['skipped_today']) == (False, False)


async def add_catalogue(session):
    session.add_all([
        RoutineTemplateCategory(id='morning', name_i18n={'ko': '아침', 'en': 'Morning', 'ja': '朝'}, sort_order=1),
        RoutineTemplateCategory(id='home', name_i18n={'ko': '생활', 'en': 'Home'}, sort_order=2),
        RoutineTemplateCategory(id='retired', name_i18n={'ko': '없음'}, sort_order=0, is_active=False),
    ])
    await session.flush()
    session.add_all([
        RoutineTemplate(id='make_bed', category_id='morning', name_i18n={'ko': '이불 정리하기', 'en': 'Make the bed'},
                        icon='bed', color='peach', days_of_week=[1, 2, 3, 4, 5, 6, 7], is_recommended=True, sort_order=1),
        RoutineTemplate(id='drink_water', category_id='morning', name_i18n={'ko': '물 마시기', 'en': 'Drink water'},
                        icon='droplet', color='blue', days_of_week=[1, 2, 3, 4, 5, 6, 7], is_recommended=True, sort_order=2),
        RoutineTemplate(id='do_laundry', category_id='home', name_i18n={'ko': '빨래하기', 'en': 'Do the laundry'},
                        icon='t_shirt', color='blue', days_of_week=[7], sort_order=1),
        RoutineTemplate(id='old_one', category_id='home', name_i18n={'ko': '옛것'}, icon='memo',
                        color='mint', days_of_week=[1], sort_order=2, is_active=False),
        RoutineTemplate(id='hidden', category_id='retired', name_i18n={'ko': '숨김'}, icon='memo',
                        color='mint', days_of_week=[1], sort_order=1),
    ])
    await session.commit()


async def test_template_selection_creates_once_in_catalogue_order(session):
    await add_catalogue(session)
    PROFILES.setdefault(UID, SimpleNamespace(id=UID, timezone='Asia/Seoul', language='en',
                                             routine_template_selection_at=None))
    listing = await routine.routine_templates(session, str(UID))
    assert listing['selection_completed'] is False
    assert [(c['id'], [t['id'] for t in c['templates']]) for c in listing['categories']] == [
        ('morning', ['make_bed', 'drink_water']), ('home', ['do_laundry']),
    ]
    today = seoul(2026, 9, 17)
    created = await routine.select_templates(
        session, str(UID), RoutineTemplateSelectionRequest(template_ids=['do_laundry', 'make_bed']), today,
    )
    assert [(r['template_id'], r['name']) for r in created['data']] == [
        ('make_bed', 'Make the bed'), ('do_laundry', 'Do the laundry'),
    ]
    assert [r['template_id'] for r in (await routine.list_routines(session, str(UID), today))['data']] == [
        'make_bed', 'do_laundry',
    ]
    for item in created['data']:
        assert (await schedules(session, uuid.UUID(item['id'])))[0][0] == date(2026, 9, 17)
    again = await routine.select_templates(
        session, str(UID), RoutineTemplateSelectionRequest(template_ids=['make_bed', 'drink_water']), today,
    )
    assert [r['template_id'] for r in again['data']] == ['drink_water']
    with pytest.raises(AppError):
        await routine.select_templates(
            session, str(UID), RoutineTemplateSelectionRequest(template_ids=['old_one']), today,
        )
    with pytest.raises(AppError):
        await routine.select_templates(
            session, str(UID), RoutineTemplateSelectionRequest(template_ids=['hidden']), today,
        )
    listing = await routine.routine_templates(session, str(UID))
    assert listing['selection_completed'] is True
    assert {t['id']: t['added'] for c in listing['categories'] for t in c['templates']} == {
        'make_bed': True, 'drink_water': True, 'do_laundry': True,
    }
    await routine.delete_routine(session, str(UID), created['data'][0]['id'], today)
    listing = await routine.routine_templates(session, str(UID))
    assert listing['categories'][0]['templates'][0]['added'] is False
    recreated = await routine.select_templates(
        session, str(UID), RoutineTemplateSelectionRequest(template_ids=['make_bed']), today,
    )
    assert [r['template_id'] for r in recreated['data']] == ['make_bed']
    assert await session.scalar(select(func.count()).select_from(Routine).where(Routine.user_id == UID)) == 4
