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


@pytest_asyncio.fixture
async def share_fence_connections(online_schema_connections):
    """Only UUID-owned tables; third connection observes concurrent statements."""
    first, second, table = online_schema_connections
    path = os.environ['MOLY_CHECKPOINT_ENV_FILE']
    dsn = load_conn(path)
    assert_dev_target(path, dsn)
    third = await asyncpg.connect(dsn, statement_cache_size=0, command_timeout=5)
    child = table + '_fk'
    try:
        yield first, second, third, table, child
    finally:
        for connection in (first, second, third):
            if connection.is_in_transaction():
                await connection.execute('ROLLBACK')
        await first.execute(f'DROP TABLE IF EXISTS public.{child}')
        assert await first.fetchval('SELECT to_regclass($1) IS NULL', 'public.'+child)
        await third.close()


async def test_share_fence_owner_can_publish_insert_and_update(share_fence_connections):
    first, second, _, table, _ = share_fence_connections
    transaction = first.transaction()
    await transaction.start()
    try:
        async with asyncio.timeout(1):
            await first.execute(f'LOCK TABLE public.{table} IN SHARE MODE NOWAIT')
            await first.execute(f'INSERT INTO public.{table} VALUES(1001)')
            await first.execute(f'UPDATE public.{table} SET value=2000 WHERE value=1000')
            # AccessShare remains compatible while the publication TX is open.
            assert await second.fetchval(f'SELECT count(*) FROM public.{table}') == 1000
            await transaction.commit()
        assert await second.fetchval(f'SELECT count(*) FROM public.{table}') == 1001
        assert await second.fetchval(f'SELECT count(*) FROM public.{table} WHERE value=2000') == 1
    finally:
        if first.is_in_transaction():
            await first.execute('ROLLBACK')


@pytest.mark.parametrize('operation,expected_count', [('insert', 1001), ('update', 1000), ('delete', 999)])
async def test_external_dml_waits_then_finishes_unchanged_after_fence_release(
    share_fence_connections, operation, expected_count,
):
    first, second, third, table, _ = share_fence_connections
    # Transaction pooling can expose a proxy startup PID. Pin the real backend
    # before observing its pending DML from a third connection.
    writer = second.transaction()
    await writer.start()
    backend = await second.fetchval('SELECT pg_backend_pid()')
    statements = {
        'insert': f'INSERT INTO public.{table} VALUES(1001)',
        'update': f'UPDATE public.{table} SET value=2000 WHERE value=1000',
        'delete': f'DELETE FROM public.{table} WHERE value=1000',
    }
    transaction, pending = first.transaction(), None
    await transaction.start()
    try:
        async with asyncio.timeout(1):
            await first.execute(f'LOCK TABLE public.{table} IN SHARE MODE NOWAIT')
            pending = asyncio.create_task(second.execute(statements[operation]))
            # Verify a real lock wait rather than relying on a timed sleep.
            waiting = False
            for _ in range(12):
                waiting = await third.fetchval('SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND NOT granted)', backend)
                if waiting:
                    break
                await asyncio.sleep(0.01)
            assert waiting and not pending.done()
            assert await third.fetchval(f'SELECT count(*) FROM public.{table}') == 1000
            await transaction.rollback()
            await pending
            await writer.commit()
        assert await third.fetchval(f'SELECT count(*) FROM public.{table}') == expected_count
        if operation == 'update':
            assert await third.fetchval(f'SELECT count(*) FROM public.{table} WHERE value=2000') == 1
    finally:
        if first.is_in_transaction():
            await first.execute('ROLLBACK')
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if second.is_in_transaction():
            await second.execute('ROLLBACK')


async def test_share_fence_allows_external_rowshare_and_foreign_key_read(share_fence_connections):
    first, second, third, table, child = share_fence_connections
    await first.execute(f'ALTER TABLE public.{table} ADD PRIMARY KEY(value)')
    await first.execute(f'CREATE TABLE public.{child}(value integer REFERENCES public.{table}(value))')
    transaction = first.transaction()
    await transaction.start()
    try:
        async with asyncio.timeout(1):
            await first.execute(f'LOCK TABLE public.{table} IN SHARE MODE NOWAIT')
            assert await second.fetchval(f'SELECT value FROM public.{table} WHERE value=1 FOR KEY SHARE') == 1
            # The child is deliberately not fenced: its FK needs RowShare/KeyShare
            # on the parent, which a parent-table SHARE fence must still allow.
            await third.execute(f'INSERT INTO public.{child} VALUES(1)')
            assert await first.fetchval(f'SELECT count(*) FROM public.{child}') == 1
            await transaction.rollback()
    finally:
        if first.is_in_transaction():
            await first.execute('ROLLBACK')


async def test_share_fence_nowait_refuses_an_existing_writer_without_delaying_it(share_fence_connections):
    first, second, third, table, _ = share_fence_connections
    writer, fence = second.transaction(), first.transaction()
    await writer.start()
    await second.execute(f'INSERT INTO public.{table} VALUES(1001)')
    await fence.start()
    try:
        async with asyncio.timeout(1):
            with pytest.raises(asyncpg.LockNotAvailableError):
                await first.execute(f'LOCK TABLE public.{table} IN SHARE MODE NOWAIT')
            await fence.rollback()
            assert await third.fetchval(f'SELECT count(*) FROM public.{table}') == 1000
            await writer.commit()
        assert await third.fetchval(f'SELECT count(*) FROM public.{table}') == 1001
    finally:
        for connection in (first, second):
            if connection.is_in_transaction():
                await connection.execute('ROLLBACK')
