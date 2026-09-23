"""Real FK/cascade checks: access transfers must not erase another user's finances.

Run only against a disposable local moly_schema_* DB after the transfer migration.
Every test rolls back its rows. No production or remote DSN is accepted.
"""
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
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
    transaction = conn.transaction()
    await transaction.start()
    try:
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()


async def signup(conn):
    user_id = uuid.uuid4()
    await conn.execute(
        'INSERT INTO auth.users(id,created_at) VALUES($1,$2)',
        user_id, datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    return user_id


async def create_subscription(conn, owner):
    return await conn.fetchval('''
        INSERT INTO public.subscriptions(user_id,plan,status,original_transaction_id,
                                         environment,expires_at)
        VALUES($1,'monthly','active',$2,'SANDBOX',now()+interval '1 month') RETURNING id
    ''', owner, f'transfer-test-{uuid.uuid4()}')


async def create_payment(conn, owner, subscription_id):
    return await conn.fetchval('''
        INSERT INTO public.payments(user_id,subscription_id,store,store_transaction_id,
                                    amount,currency,status,paid_at)
        VALUES($1,$2,'app_store',$3,8,'USD','paid',now()) RETURNING id
    ''', owner, subscription_id, f'transfer-paid-{uuid.uuid4()}')


async def delete_user(conn, user_id):
    await conn.execute('DELETE FROM auth.users WHERE id=$1', user_id)
    # Also exercise deferred cleanup if the migration implements a constraint trigger.
    await conn.execute('SET CONSTRAINTS ALL IMMEDIATE')


@pytest.mark.parametrize('delete_access_owner_first', [True, False])
async def test_delete_order_preserves_surviving_owner_payment(connection, delete_access_owner_first):
    original_owner, new_owner = await signup(connection), await signup(connection)
    sub_id = await create_subscription(connection, original_owner)
    old_payment = await create_payment(connection, original_owner, sub_id)
    await connection.execute('UPDATE public.subscriptions SET user_id=$1 WHERE id=$2', new_owner, sub_id)
    new_payment = await create_payment(connection, new_owner, sub_id)
    survivor = original_owner if delete_access_owner_first else new_owner
    deleted = new_owner if delete_access_owner_first else original_owner
    surviving_payment = old_payment if delete_access_owner_first else new_payment
    deleted_payment = new_payment if delete_access_owner_first else old_payment

    await delete_user(connection, deleted)

    assert await connection.fetchval('SELECT user_id FROM public.subscriptions WHERE id=$1', sub_id) == (
        None if delete_access_owner_first else new_owner
    )
    payment = await connection.fetchrow('SELECT * FROM public.payments WHERE id=$1', surviving_payment)
    assert payment is not None
    assert payment['user_id'] == survivor
    assert payment['subscription_id'] == sub_id
    assert payment['amount'] == Decimal('8.0000')
    assert payment['status'] == 'paid'
    assert not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM public.payments WHERE id=$1)', deleted_payment)

    await delete_user(connection, survivor)
    assert not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM public.subscriptions WHERE id=$1)', sub_id)
    assert not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM public.payments WHERE subscription_id=$1)', sub_id)


async def test_trial_without_payment_is_removed_after_access_owner_delete(connection):
    owner = await signup(connection)
    sub_id = await create_subscription(connection, owner)
    await delete_user(connection, owner)
    assert not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM public.subscriptions WHERE id=$1)', sub_id)


async def test_original_owner_delete_keeps_transferred_trial_access(connection):
    original_owner, new_owner = await signup(connection), await signup(connection)
    sub_id = await create_subscription(connection, original_owner)
    await connection.execute('UPDATE public.subscriptions SET user_id=$1 WHERE id=$2', new_owner, sub_id)
    await delete_user(connection, original_owner)
    row = await connection.fetchrow('SELECT * FROM public.subscriptions WHERE id=$1', sub_id)
    assert row is not None
    assert row['user_id'] == new_owner
    assert row['status'] == 'active'
    await delete_user(connection, new_owner)
    assert not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM public.subscriptions WHERE id=$1)', sub_id)


async def test_access_owner_delete_keeps_original_payment_until_last_financial_owner_delete(connection):
    original_owner, new_owner = await signup(connection), await signup(connection)
    sub_id = await create_subscription(connection, original_owner)
    payment_id = await create_payment(connection, original_owner, sub_id)
    await connection.execute('UPDATE public.subscriptions SET user_id=$1 WHERE id=$2', new_owner, sub_id)
    await delete_user(connection, new_owner)
    row = await connection.fetchrow('SELECT * FROM public.subscriptions WHERE id=$1', sub_id)
    assert row is not None and row['user_id'] is None
    assert await connection.fetchval('SELECT user_id FROM public.payments WHERE id=$1', payment_id) == original_owner
    await delete_user(connection, original_owner)
    assert not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM public.subscriptions WHERE id=$1)', sub_id)


async def test_payment_snapshot_and_exact_grant_survive_plan_change(connection):
    original_owner, new_owner = await signup(connection), await signup(connection)
    sub_id = await create_subscription(connection, original_owner)
    grant_id = await connection.fetchval('''
        INSERT INTO public.subscription_hay_grants(user_id,plan)
        VALUES($1,'monthly') RETURNING id
    ''', original_owner)
    payment_id = await create_payment(connection, original_owner, sub_id)
    await connection.execute('''
        UPDATE public.payments SET subscription_plan='monthly',
          subscription_hay_grant_id=$1, subscription_bonus_review='granted' WHERE id=$2
    ''', grant_id, payment_id)
    await connection.execute('''
        UPDATE public.subscriptions SET user_id=$1,plan='yearly' WHERE id=$2
    ''', new_owner, sub_id)
    row = await connection.fetchrow('SELECT * FROM public.payments WHERE id=$1', payment_id)
    assert row['user_id'] == original_owner
    assert row['subscription_plan'] == 'monthly'
    assert row['subscription_hay_grant_id'] == grant_id
    await connection.execute('DELETE FROM public.subscription_hay_grants WHERE id=$1', grant_id)
    row = await connection.fetchrow('SELECT * FROM public.payments WHERE id=$1', payment_id)
    assert row['subscription_hay_grant_id'] is None
    assert row['subscription_plan'] == 'monthly'
    assert row['subscription_bonus_review'] == 'granted'


async def test_deferred_cleanup_allows_atomic_owner_reassignment(connection):
    original_owner, new_owner = await signup(connection), await signup(connection)
    sub_id = await create_subscription(connection, original_owner)
    await connection.execute('UPDATE public.subscriptions SET user_id=NULL WHERE id=$1', sub_id)
    await connection.execute('UPDATE public.subscriptions SET user_id=$1 WHERE id=$2', new_owner, sub_id)
    await connection.execute('SET CONSTRAINTS ALL IMMEDIATE')
    assert await connection.fetchval('SELECT user_id FROM public.subscriptions WHERE id=$1', sub_id) == new_owner


async def test_cleanup_trigger_drops_supabase_default_service_role_execute(connection):
    # Supabase grants service_role EXECUTE by default, unlike a plain PostgreSQL fixture.
    await connection.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO service_role')
    await connection.execute('DROP FUNCTION public.cleanup_unowned_subscription() CASCADE')
    migration = (Path(__file__).parents[2] / 'db/changes/subscription_transfer.sql').read_text()
    # Keep this test's rollback boundary; execute the actual migration body within it.
    body = migration.split('\nBEGIN;\n', 1)[1].rsplit('\nCOMMIT;', 1)[0]
    await connection.execute(body)
    for role in ('anon', 'authenticated', 'service_role'):
        assert not await connection.fetchval(
            "SELECT has_function_privilege($1,'public.cleanup_unowned_subscription()','EXECUTE')", role,
        )
    owner = await signup(connection)
    sub_id = await create_subscription(connection, owner)
    await delete_user(connection, owner)
    assert not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM public.subscriptions WHERE id=$1)', sub_id)
