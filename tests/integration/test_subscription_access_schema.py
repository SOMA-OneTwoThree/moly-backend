"""Member classification by signup time: free_launch_until is the release moment."""
from datetime import datetime, timedelta, timezone
import json
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

from db.schema_contract import require_scratch

NOW = datetime.now(timezone.utc)
NEW_MEMBER_SINCE = NOW - timedelta(days=10)
OFFERS = {plan: {'ready': True, 'product_id': f'fixture.{plan}', 'base_plan_id': plan,
                 'offer_id': f'fixture-{plan}'} for plan in ('monthly', 'yearly')}


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


async def configure(conn, launch_until, offer_expires=NOW + timedelta(days=30)):
    for key, value in {
        'free_launch_until': launch_until.isoformat(),
        'subscription_launch': {'new_member_since': NEW_MEMBER_SINCE.isoformat(), 'campaign_id': 'fixture',
                                'legacy_offer_expires_at': offer_expires.isoformat(),
                                'offers': {'android': OFFERS}},
    }.items():
        await conn.execute('''
            INSERT INTO public.app_config(key,value) VALUES($1,$2::jsonb)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        ''', key, json.dumps(value))


async def member(conn, created_at):
    uid = uuid.uuid4()
    await conn.execute('INSERT INTO auth.users(id,created_at) VALUES($1,$2)', uid, created_at)
    await conn.execute("UPDATE public.profiles SET nickname='test' WHERE id=$1", uid)
    return uid


async def access(conn, uid):
    return json.loads(await conn.fetchval('SELECT public.subscription_launch_access($1)', uid))


def classified(self_trial, legacy):
    return {'enabled': True, 'self_trial_available': self_trial, 'legacy_offer_eligible': legacy}


async def test_before_release_testers_reach_both_paywalls(connection):
    await configure(connection, NOW + timedelta(days=30))
    existing = await member(connection, NEW_MEMBER_SINCE - timedelta(seconds=1))
    fresh = await member(connection, NOW - timedelta(hours=1))
    reviewer = await member(connection, NEW_MEMBER_SINCE)
    assert await access(connection, existing) == classified(False, True)
    assert await access(connection, fresh) == classified(True, False)
    # A new-member demo account made days before review still gets the plain paywall.
    assert await access(connection, reviewer) == classified(False, False)


async def test_release_makes_every_earlier_account_an_existing_member(connection):
    launch = NOW - timedelta(hours=1)
    await configure(connection, launch)
    before = await member(connection, launch - timedelta(microseconds=1))
    after = await member(connection, launch)
    assert await access(connection, before) == classified(False, True)
    assert await access(connection, after) == classified(True, False)


async def test_new_member_starts_signup_trial_once_and_existing_member_cannot(connection):
    await configure(connection, NOW + timedelta(days=30))
    created = NOW - timedelta(hours=3)
    uid = await member(connection, created)
    await connection.execute('SELECT public.start_subscription_trial($1)', uid)
    row = await connection.fetchrow('SELECT app_trial_started_at,app_trial_ends_at FROM public.profiles WHERE id=$1', uid)
    assert row['app_trial_started_at'] == created
    assert row['app_trial_ends_at'] == created + timedelta(hours=48)
    await connection.execute('SELECT public.start_subscription_trial($1)', uid)
    assert await connection.fetchval('SELECT app_trial_started_at FROM public.profiles WHERE id=$1', uid) == created
    assert await access(connection, uid) == classified(False, False)
    existing = await member(connection, NEW_MEMBER_SINCE - timedelta(days=1))
    with pytest.raises(asyncpg.RaiseError):
        async with connection.transaction():
            await connection.execute('SELECT public.start_subscription_trial($1)', existing)


async def test_started_app_trial_never_turns_into_the_store_offer(connection):
    await configure(connection, NOW + timedelta(days=30))
    uid = await member(connection, NEW_MEMBER_SINCE - timedelta(days=1))
    await connection.execute('UPDATE public.profiles SET app_trial_started_at=$1, app_trial_ends_at=$2 WHERE id=$3',
                             NOW - timedelta(days=5), NOW - timedelta(days=3), uid)
    assert await access(connection, uid) == classified(False, False)


async def test_active_subscription_or_elapsed_offer_closes_offers(connection):
    await configure(connection, NOW + timedelta(days=30), offer_expires=NOW - timedelta(seconds=1))
    expired_offer = await member(connection, NEW_MEMBER_SINCE - timedelta(days=1))
    assert await access(connection, expired_offer) == classified(False, False)
    await configure(connection, NOW + timedelta(days=30))
    subscribed = await member(connection, NOW - timedelta(hours=1))
    await connection.execute('''
        INSERT INTO public.subscriptions(user_id,plan,status,original_transaction_id,expires_at)
        VALUES($1,'monthly','active',$2,$3)
    ''', subscribed, str(uuid.uuid4()), NOW + timedelta(days=30))
    assert await access(connection, subscribed) == classified(False, False)


async def test_existing_member_claims_android_offer_only(connection):
    await configure(connection, NOW + timedelta(days=30))
    uid = await member(connection, NEW_MEMBER_SINCE - timedelta(days=1))
    status = json.loads(await connection.fetchval('SELECT public.subscription_offer_status($1)', uid))
    assert status['legacy_offer_eligible'] and status['android_offer_ready'] and not status['ios_offer_ready']
    with pytest.raises(asyncpg.RaiseError):
        async with connection.transaction():
            await connection.execute("SELECT public.claim_subscription_offer($1,'ios','monthly')", uid)
    first = await connection.fetchval("SELECT public.claim_subscription_offer($1,'android','monthly')", uid)
    assert await connection.fetchval("SELECT public.claim_subscription_offer($1,'android','monthly')", uid) == first
    yearly = json.loads(await connection.fetchval("SELECT public.claim_subscription_offer($1,'android','yearly')", uid))
    assert yearly['base_plan_id'] == 'yearly'
    await connection.execute('UPDATE public.subscription_offer_claims SET redeemed_at=now() WHERE user_id=$1', uid)
    with pytest.raises(asyncpg.RaiseError):
        async with connection.transaction():
            await connection.execute("SELECT public.claim_subscription_offer($1,'android','yearly')", uid)
    fresh = await member(connection, NOW - timedelta(hours=1))
    with pytest.raises(asyncpg.RaiseError):
        async with connection.transaction():
            await connection.execute("SELECT public.claim_subscription_offer($1,'android','monthly')", fresh)
