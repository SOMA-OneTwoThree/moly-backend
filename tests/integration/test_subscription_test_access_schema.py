"""Account-scoped pre-release enrollment, tested only on disposable local PostgreSQL."""
from datetime import datetime, timedelta, timezone
import json
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

from db.schema_contract import require_scratch


@pytest_asyncio.fixture
async def connection():
    dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('MOLY_SCHEMA_TEST_DSN is required for local schema tests')
    require_scratch(dsn)
    conn = await asyncpg.connect(dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def configure(conn, mode='regular', expiry=None):
    uid = uuid.uuid4()
    await conn.execute('INSERT INTO auth.users(id) VALUES($1)', uid)
    await conn.execute("UPDATE public.profiles SET nickname='test' WHERE id=$1", uid)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace('+00:00', 'Z')
    value = {'accounts': {str(uid): mode}, 'expires_at': expiry if expiry is not None else future,
             'campaign_id': 'schema-only-preview'}
    await conn.execute("DELETE FROM public.app_config WHERE key='subscription_launch'")
    await conn.execute('''
        INSERT INTO public.app_config(key,value) VALUES('subscription_launch_test',$1::jsonb)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    ''', json.dumps(value))
    return uid


async def access(conn, uid):
    return json.loads(await conn.fetchval('SELECT public.subscription_launch_access($1)', uid))


@pytest.mark.parametrize('mode', ['regular', 'legacy_offer'])
async def test_explicit_account_mode_and_self_trial(connection, mode):
    uid = await configure(connection, mode)
    assert await access(connection, uid) == {'enabled': True, 'legacy_offer_eligible': mode == 'legacy_offer'}
    assert (await access(connection, uuid.uuid4()))['enabled'] is False
    await connection.execute('SELECT public.start_subscription_trial($1)', uid)
    first = await connection.fetchrow('SELECT app_trial_started_at,app_trial_ends_at FROM public.profiles WHERE id=$1', uid)
    assert first['app_trial_ends_at'] - first['app_trial_started_at'] == timedelta(hours=48)
    await connection.execute('SELECT public.start_subscription_trial($1)', uid)
    assert await connection.fetchval('SELECT app_trial_started_at FROM public.profiles WHERE id=$1', uid) == first['app_trial_started_at']
    status = json.loads(await connection.fetchval('SELECT public.subscription_offer_status($1)', uid))
    assert not status['ios_offer_ready'] and not status['android_offer_ready']


@pytest.mark.parametrize('expiry', ['invalid', 'infinity', '2099-01-01', '2020-01-01T00:00:00Z'])
async def test_invalid_or_expired_preview_denies_mutations(connection, expiry):
    uid = await configure(connection, 'legacy_offer', expiry)
    assert not (await access(connection, uid))['enabled']
    with pytest.raises(asyncpg.RaiseError):
        async with connection.transaction():
            await connection.execute('SELECT public.start_subscription_trial($1)', uid)
    with pytest.raises(asyncpg.RaiseError):
        async with connection.transaction():
            await connection.execute("SELECT public.claim_subscription_offer($1,'ios','monthly')", uid)


async def test_production_flag_prevents_test_override_even_with_invalid_cutoff(connection):
    uid = await configure(connection)
    await connection.execute("INSERT INTO public.app_config(key,value) VALUES('subscription_launch','{\"enabled\":true}'::jsonb)")
    assert not (await access(connection, uid))['enabled']


async def test_preview_cannot_reuse_production_campaign(connection):
    uid = await configure(connection, 'legacy_offer')
    await connection.execute("INSERT INTO public.app_config(key,value) VALUES('subscription_launch','{\"enabled\":false,\"campaign_id\":\"schema-only-preview\"}'::jsonb)")
    assert not (await access(connection, uid))['enabled']
