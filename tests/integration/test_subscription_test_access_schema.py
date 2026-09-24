"""Only explicit, expiring account previews bypass the disabled global rollout."""
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
    await conn.execute("INSERT INTO auth.users(id,created_at) VALUES($1,'2020-01-01T00:00:00Z')", uid)
    await conn.execute("UPDATE public.profiles SET nickname='test' WHERE id=$1", uid)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace('+00:00', 'Z')
    value = {'accounts': {str(uid): mode}, 'expires_at': expiry if expiry is not None else future,
             'campaign_id': 'schema-only-preview'}
    await conn.execute("INSERT INTO public.app_config(key,value) VALUES('subscription_launch','{\"enabled\":false}'::jsonb) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    await conn.execute('''
        INSERT INTO public.app_config(key,value) VALUES('subscription_launch_test',$1::jsonb)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    ''', json.dumps(value))
    return uid


async def access(conn, uid):
    return json.loads(await conn.fetchval('SELECT public.subscription_launch_access($1)', uid))


@pytest.mark.parametrize('mode', ['regular', 'legacy_offer'])
async def test_explicit_account_mode_starts_self48_once(connection, mode):
    uid = await configure(connection, mode)
    before = await connection.fetchval('SELECT created_at FROM auth.users WHERE id=$1', uid)
    assert await access(connection, uid) == {'enabled': True, 'legacy_offer_eligible': mode == 'legacy_offer'}
    assert (await access(connection, uuid.uuid4()))['enabled'] is False
    await connection.execute('SELECT public.start_subscription_trial($1)', uid)
    row = await connection.fetchrow('SELECT app_trial_started_at,app_trial_ends_at FROM public.profiles WHERE id=$1', uid)
    assert row['app_trial_ends_at'] - row['app_trial_started_at'] == timedelta(hours=48)
    assert row['app_trial_started_at'] > before + timedelta(days=1)
    await connection.execute('SELECT public.start_subscription_trial($1)', uid)
    assert await connection.fetchval('SELECT app_trial_started_at FROM public.profiles WHERE id=$1', uid) == row['app_trial_started_at']
    assert await connection.fetchval('SELECT created_at FROM auth.users WHERE id=$1', uid) == before
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
    await connection.execute("INSERT INTO public.app_config(key,value) VALUES('subscription_launch','{\"enabled\":true}'::jsonb) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    assert not (await access(connection, uid))['enabled']


async def test_preview_cannot_reuse_production_campaign(connection):
    uid = await configure(connection, 'legacy_offer')
    await connection.execute("INSERT INTO public.app_config(key,value) VALUES('subscription_launch','{\"enabled\":false,\"campaign_id\":\"schema-only-preview\"}'::jsonb) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    assert not (await access(connection, uid))['enabled']


async def test_missing_live_config_and_future_enabled_cutoff_do_not_allow_preview(connection):
    uid = await configure(connection)
    await connection.execute("DELETE FROM public.app_config WHERE key='subscription_launch'")
    assert not (await access(connection, uid))['enabled']
    await connection.execute("INSERT INTO public.app_config(key,value) VALUES('subscription_launch','{\"enabled\":true,\"existing_user_cutoff\":\"2099-01-01T00:00:00Z\"}'::jsonb)")
    assert not (await access(connection, uid))['enabled']


async def test_preview_keeps_ios_offer_id_inventory_and_once_checks(connection):
    uid = await configure(connection, 'legacy_offer')
    offers = {plan: {'ready': True, 'product_id': f'fixture.{plan}'} for plan in ('monthly', 'yearly')}
    await connection.execute("UPDATE public.app_config SET value=value || $1::jsonb WHERE key='subscription_launch_test'",
                             json.dumps({'apple_app_id': '12345', 'offers': {'ios': offers}}))
    for plan in offers:
        await connection.execute("INSERT INTO public.subscription_trial_codes(campaign_id,plan,code,expires_at) VALUES('schema-only-preview',$1,$2,now()+interval '1 day')", plan, str(uuid.uuid4()))
    status = json.loads(await connection.fetchval('SELECT public.subscription_offer_status($1)', uid))
    assert status['legacy_offer_eligible'] and not status['ios_offer_ready']
    with pytest.raises(asyncpg.RaiseError):
        async with connection.transaction():
            await connection.execute("SELECT public.claim_subscription_offer($1,'ios','monthly')", uid)
    for plan in offers:
        offers[plan]['offer_id'] = f'fixture-{plan}'
    await connection.execute("UPDATE public.app_config SET value=value || $1::jsonb WHERE key='subscription_launch_test'",
                             json.dumps({'offers': {'ios': offers}}))
    assert json.loads(await connection.fetchval('SELECT public.subscription_offer_status($1)', uid))['ios_offer_ready']
    first = await connection.fetchval("SELECT public.claim_subscription_offer($1,'ios','monthly')", uid)
    assert await connection.fetchval("SELECT public.claim_subscription_offer($1,'ios','monthly')", uid) == first
    await connection.execute('UPDATE public.subscription_offer_claims SET redeemed_at=now() WHERE user_id=$1', uid)
    with pytest.raises(asyncpg.RaiseError):
        async with connection.transaction():
            await connection.execute("SELECT public.claim_subscription_offer($1,'ios','monthly')", uid)


async def test_live_enrollment_keeps_signup_clock(connection):
    uid = await configure(connection)
    created = datetime.now(timezone.utc) - timedelta(hours=3)
    cutoff = created - timedelta(hours=1)
    await connection.execute('UPDATE auth.users SET created_at=$1 WHERE id=$2', created, uid)
    await connection.execute("UPDATE public.app_config SET value=$1::jsonb WHERE key='subscription_launch'",
                             json.dumps({'enabled': True, 'existing_user_cutoff': cutoff.isoformat()}))
    await connection.execute('SELECT public.start_subscription_trial($1)', uid)
    row = await connection.fetchrow('SELECT app_trial_started_at,app_trial_ends_at FROM public.profiles WHERE id=$1', uid)
    assert row['app_trial_started_at'] == created
    assert row['app_trial_ends_at'] == created + timedelta(hours=48)
