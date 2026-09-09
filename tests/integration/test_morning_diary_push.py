"""Actual development DB selection and deduplication; FCM is always replaced."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.diary import Diary
from app.services import notify, push
from db.envfile import assert_dev_target, load_conn
from db.schema_contract import require_scratch

NOW = datetime(2026, 9, 9, 0, tzinfo=timezone.utc)
TARGET = NOW.date() - timedelta(days=1)


@pytest_asyncio.fixture
async def morning_db(monkeypatch):
    if os.environ.get('MOLY_NOTIFICATION_TEST_ENV') == 'dev':
        if os.environ.get('MOLY_SCHEMA_TEST_DSN'):
            pytest.fail('choose dev or scratch, not both')
        dsn = load_conn('dev')
        assert_dev_target('dev', dsn)
    else:
        dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
        if not dsn:
            pytest.skip('explicit notification dev or scratch database required')
        require_scratch(dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=15, command_timeout=30)
    engine = create_async_engine(make_url(dsn).set(drivername='postgresql+asyncpg'),
                                 connect_args={'statement_cache_size': 0})
    uid = uuid.uuid4()
    send = AsyncMock(return_value=1)
    monkeypatch.setattr(push, 'send', send)
    monkeypatch.setattr(push, 'prepare_access_token', AsyncMock(return_value='fake-test-access'))
    monkeypatch.setattr(notify.settings, 'morning_push_enabled', True)
    profile = SimpleNamespace(id=uid, timezone='Asia/Seoul', language='ko')
    try:
        await conn.execute('INSERT INTO auth.users(id,created_at) VALUES($1,now())', uid)
        # Never use real device credentials or issue a real push in an integration test.
        monkeypatch.setattr(notify, '_tokens', AsyncMock(return_value=['integration-test-only']))
        yield SimpleNamespace(conn=conn, engine=engine, uid=uid, profile=profile, send=send)
    finally:
        await conn.execute('DELETE FROM auth.users WHERE id=$1', uid)
        remaining = await conn.fetchval(
            'SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=$1 '
            'UNION ALL SELECT 1 FROM diaries WHERE user_id=$1 '
            'UNION ALL SELECT 1 FROM user_daily_stats WHERE user_id=$1)', uid,
        )
        await engine.dispose()
        await conn.close()
        assert not remaining, 'temporary notification data remains'


async def create_diary(db, **overrides):
    fields = dict(id=uuid.uuid4(), user_id=db.uid, diary_date=TARGET, activity_date=TARGET,
                  display_date=TARGET, kind='shared_day', source='llm', content='테스트 일기',
                  weather='sunny', published_at=NOW, record_status='published')
    fields.update(overrides)
    async with AsyncSession(db.engine) as session:
        row = Diary(**fields)
        session.add(row)
        await session.commit()
    return fields['id']


async def deliver(db, now=NOW):
    async with AsyncSession(db.engine) as session:
        return await notify.notify_morning(session, db.profile, now=now)


async def marker(db):
    return await db.conn.fetchval(
        'SELECT morning_notified_at FROM user_daily_stats WHERE user_id=$1 AND activity_date=$2',
        db.uid, NOW.date(),
    )


@pytest.mark.parametrize('kind,source', [('shared_day', 'llm'), ('capi_day', 'preset')])
async def test_parallel_requests_claim_once_and_link_exact_diary(morning_db, kind, source):
    db = morning_db
    diary_id = await create_diary(db, kind=kind, source=source)
    assert sorted(await asyncio.gather(deliver(db), deliver(db))) == [0, 1]
    db.send.assert_awaited_once()
    assert db.send.call_args.kwargs['data'] == {'link': 'diary', 'diary_id': str(diary_id)}
    assert await marker(db) is not None
    assert await deliver(db, NOW + timedelta(minutes=15)) == 0


@pytest.mark.parametrize('case', ['absent', 'read', 'future', 'deleted', 'old', 'welcome', 'draft', 'no_entry'])
async def test_no_new_unread_published_diary_does_not_consume_slot(morning_db, case):
    db = morning_db
    if case == 'read':
        await create_diary(db, first_read_at=NOW)
    if case == 'future':
        await create_diary(db, published_at=NOW + timedelta(minutes=15))
    if case == 'deleted':
        await create_diary(db, deleted_at=NOW, record_status='deleted')
    if case == 'old':
        old_date = TARGET - timedelta(days=1)
        await create_diary(db, activity_date=old_date, diary_date=old_date, display_date=old_date,
                           published_at=NOW-timedelta(days=1))
    if case == 'welcome':
        await db.conn.execute("DELETE FROM diaries WHERE user_id=$1 AND kind='welcome'", db.uid)
        await create_diary(db, kind='welcome', source='welcome', activity_date=None)
    if case == 'draft':
        await create_diary(db, record_status='draft')
    if case == 'no_entry':
        await create_diary(db, kind=None, source='none', published_at=None, record_status='processed')
    assert await deliver(db) == 0
    db.send.assert_not_awaited()
    assert await marker(db) is None


async def test_no_diary_or_device_can_be_reconsidered_within_morning(morning_db, monkeypatch):
    db = morning_db
    assert await deliver(db) == 0
    await create_diary(db)
    monkeypatch.setattr(notify, '_tokens', AsyncMock(return_value=[]))
    assert await deliver(db) == 0
    assert await marker(db) is None
    monkeypatch.setattr(notify, '_tokens', AsyncMock(return_value=['integration-test-only']))
    assert await deliver(db, NOW + timedelta(minutes=45)) == 1
    assert await deliver(db, NOW + timedelta(hours=1)) == 0


async def test_disabled_preference_and_failed_delivery(morning_db):
    db = morning_db
    await create_diary(db)
    await db.conn.execute(
        "INSERT INTO user_notification_settings(user_id,type,enabled) VALUES($1,'morning_diary',false) "
        'ON CONFLICT(user_id,type) DO UPDATE SET enabled=false', db.uid,
    )
    assert await deliver(db) == 0
    assert await marker(db) is None
    await db.conn.execute("UPDATE user_notification_settings SET enabled=true WHERE user_id=$1", db.uid)
    db.send.return_value = 0
    assert await deliver(db) == 0
    assert await marker(db) is not None
    assert await deliver(db, NOW + timedelta(minutes=15)) == 0
    db.send.assert_awaited_once()


@pytest.mark.parametrize('zone,utc_hour,utc_day', [('UTC', 9, 9), ('Pacific/Kiritimati', 19, 8), ('America/Los_Angeles', 16, 9)])
async def test_local_day_and_claim_follow_injected_tick_time(morning_db, zone, utc_hour, utc_day):
    db = morning_db
    db.profile.timezone = zone
    now = datetime(2026, 9, utc_day, utc_hour, tzinfo=timezone.utc)
    await create_diary(db, published_at=now)
    assert await deliver(db, now) == 1
    assert await marker(db) is not None
    assert db.send.call_args.kwargs['expires_at'] == now + timedelta(hours=1)
