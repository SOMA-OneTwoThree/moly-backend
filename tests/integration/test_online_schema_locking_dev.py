"""Dev PostgreSQL lock behavior for the split online schema change.

The fixture owns one UUID-named synthetic table; it never changes service data.
"""
import asyncio
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

from db.envfile import assert_dev_target, load_conn


@pytest_asyncio.fixture
async def online_schema_connections():
    if os.environ.get('MOLY_ONLINE_PROMOTION_TEST_ENV') != 'dev':
        pytest.skip('explicit development opt-in required')
    path = os.environ.get('MOLY_CHECKPOINT_ENV_FILE')
    if not path:
        pytest.skip('explicit development environment file required')
    dsn = load_conn(path)
    assert_dev_target(path, dsn)
    table = 'synthetic_online_guard_' + uuid.uuid4().hex
    first = await asyncpg.connect(dsn, statement_cache_size=0, command_timeout=5)
    second = await asyncpg.connect(dsn, statement_cache_size=0, command_timeout=5)
    try:
        await first.execute(f'CREATE TABLE public.{table}(value integer NOT NULL)')
        await first.execute(f'INSERT INTO public.{table} SELECT n FROM generate_series(1,1000) n')
        await first.execute(f'ALTER TABLE public.{table} ADD CONSTRAINT positive CHECK(value>0) NOT VALID')
        yield first, second, table
    finally:
        for connection in (first, second):
            if connection.is_in_transaction():
                await connection.execute('ROLLBACK')
        await first.execute(f'DROP TABLE IF EXISTS public.{table}')
        assert await first.fetchval('SELECT to_regclass($1) IS NULL', 'public.'+table)
        await second.close()
        await first.close()


async def test_separate_constraint_validation_does_not_block_normal_insert(online_schema_connections):
    first, second, table = online_schema_connections
    transaction = first.transaction()
    await transaction.start()
    await first.execute(f'ALTER TABLE public.{table} VALIDATE CONSTRAINT positive')
    # The validation lock is still held until commit. Ordinary writes continue.
    await asyncio.wait_for(second.execute(f'INSERT INTO public.{table} VALUES(1001)'), 1)
    await transaction.commit()
    assert await first.fetchval(f'SELECT count(*) FROM public.{table}') == 1001


async def test_busy_metadata_change_rejects_without_queueing_public_writer(online_schema_connections):
    first, second, table = online_schema_connections
    writer = second.transaction()
    await writer.start()
    await second.execute(f'INSERT INTO public.{table} VALUES(1002)')
    change = first.transaction()
    await change.start()
    with pytest.raises(asyncpg.LockNotAvailableError):
        await asyncio.wait_for(first.execute(f'LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE NOWAIT'), 1)
    await change.rollback()
    await writer.commit()
    assert await first.fetchval(f'SELECT count(*) FROM public.{table}') == 1001
