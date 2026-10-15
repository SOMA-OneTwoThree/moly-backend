"""Account deletion fence and residual sweep, executed against a disposable local DB.

The auth server calls the fence functions over PostgREST; the worker sweep finishes deletions
from the barrier row. Every handler session shares one transaction that is rolled back.
"""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import memory_pipeline, privacy
from db.schema_contract import require_scratch
from worker import privacy_sweep_jobs as sweep


class _OneTransaction:
    def __init__(self, session: AsyncSession):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def commit(self) -> None:
        await self._session.flush()


@pytest_asyncio.fixture
async def db(monkeypatch):
    dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('MOLY_SCHEMA_TEST_DSN required')
    require_scratch(dsn)
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'),
                                 poolclass=NullPool, connect_args={'statement_cache_size': 0})
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            shared = _OneTransaction(session)

            @asynccontextmanager
            async def handler_session():
                yield shared

            monkeypatch.setattr(sweep, 'get_sessionmaker', lambda: handler_session)
            yield session
            await session.rollback()
    finally:
        await engine.dispose()


async def _q(session, sql, **params):
    return [tuple(row) for row in (await session.execute(text(sql), params)).all()]


async def signup(session) -> uuid.UUID:
    uid = uuid.uuid4()
    await session.execute(text('INSERT INTO auth.users(id) VALUES (:id)'), {'id': uid})
    return uid


async def delete_account(session, uid) -> None:
    await session.execute(text('DELETE FROM auth.users WHERE id = :id'), {'id': uid})


async def add_vector(session, uid) -> None:
    await session.execute(text("""
        INSERT INTO vecs.moly_memories_v2(id, vec, metadata)
        VALUES (:id, array_fill(0::real, ARRAY[1536])::public.vector,
                jsonb_build_object('user_id', CAST(:uid AS text)))
    """), {'id': str(uuid.uuid4()), 'uid': str(uid)})


async def vectors(session, uid) -> int:
    return await session.scalar(text(
        "SELECT count(*) FROM vecs.moly_memories_v2 WHERE metadata->>'user_id' = :uid"
    ), {'uid': str(uid)})


async def barrier(session, uid):
    return (await session.execute(text(
        'SELECT state, epoch, operation_id, high_watermark FROM privacy_subject_barriers '
        'WHERE user_id = :id'
    ), {'id': uid})).one()


async def events(session, uid) -> list[str]:
    rows = await _q(session, 'SELECT event FROM privacy_ledger_events WHERE user_id = :id ORDER BY id',
                    id=uid)
    return [r[0] for r in rows]


async def begin(session, uid, operation_id=None) -> uuid.UUID:
    operation_id = operation_id or uuid.uuid4()
    await session.scalar(text('SELECT public.begin_subject_deletion(:u, :o)'),
                         {'u': uid, 'o': operation_id})
    return operation_id


async def abort(session, uid, operation_id) -> bool:
    return await session.scalar(text('SELECT public.abort_subject_deletion(:u, :o)'),
                                {'u': uid, 'o': operation_id})


async def run_sweep() -> dict:
    result = await sweep.handle_privacy_residual_sweep(SimpleNamespace(payload={}))
    return result.result_detail


async def test_fenced_deletion_redacts_then_closes_after_two_consecutive_empty_sweeps(db):
    uid = await signup(db)
    await db.execute(text('INSERT INTO chat_contexts(user_id, memory_source_watermark) VALUES (:u, 7)'),
                     {'u': uid})
    await db.execute(text("""
        INSERT INTO idempotency_keys(user_id, key, response) VALUES (:u, 'chat-key', '{"reply": "x"}')
    """), {'u': uid})
    await db.execute(text("""
        INSERT INTO async_jobs(queue, job_type, user_id, dedup_key, payload, max_attempts)
        VALUES ('memory', 'mem0_ingest', :u, 'local-ready', '{"turn_seq": 1}', 3)
    """), {'u': uid})
    operation_id = uuid.uuid4()

    assert await privacy.begin_subject_deletion(db, user_id=uid, operation_id=operation_id) == 7
    assert tuple(await barrier(db, uid)) == ('deleting', 1, operation_id, 7)
    assert await _q(db, 'SELECT response, terminal_status FROM idempotency_keys WHERE user_id = :u',
                    u=uid) == [(None, 'redacted')]
    assert await _q(db, 'SELECT state, payload, result_code FROM async_jobs WHERE user_id = :u',
                    u=uid) == [('cancelled', {}, 'subject_deleting')]
    # Serving and job finalization are blocked from here (ensure_subject_active, _SUCCESS_SQL).
    assert (await privacy.load_barrier(db, uid)).status == privacy.STATUS_DELETING

    await sweep.enqueue_sweep(db, bucket='local')
    await delete_account(db, uid)
    assert (await barrier(db, uid)).state == 'deleting'  # the fence outlives the profile
    # User jobs cascade with the profile; the sweep has no user and survives to finish the job.
    assert await _q(db, "SELECT job_type FROM async_jobs WHERE user_id = :u OR dedup_key = 'privsweep:local'",
                    u=uid) == [('privacy_residual_sweep',)]
    await add_vector(db, uid)  # a late memory write that landed after the cascade

    assert await run_sweep() == {'promoted': 0, 'subjects': 1, 'vectors_deleted': 1, 'swept': 1,
                                 'empty': 0, 'closed': 0, 'skipped': 0, 'stuck': 0}
    assert await vectors(db, uid) == 0
    assert (await run_sweep())['empty'] == 1
    assert (await barrier(db, uid)).state == 'deleting'
    assert (await run_sweep())['closed'] == 1
    assert (await barrier(db, uid)).state == 'deleted'
    assert await events(db, uid) == ['serving_blocked_and_redacted', 'residual_sweep_deleted',
                                     'residual_sweep_empty', 'subject_deleted']
    assert (await run_sweep())['subjects'] == 0


async def test_a_late_write_restarts_the_empty_sweep_count(db):
    uid = await signup(db)
    await begin(db, uid)
    await delete_account(db, uid)
    assert (await run_sweep())['empty'] == 1
    await add_vector(db, uid)
    assert (await run_sweep())['swept'] == 1
    assert (await run_sweep())['empty'] == 1  # counted again from the deletion
    assert (await barrier(db, uid)).state == 'deleting'
    assert (await run_sweep())['closed'] == 1


async def test_account_deleted_without_the_fence_is_promoted_then_closed(db):
    """moly-auth deletes the account even when the fence call fails (fail-open)."""
    uid = await signup(db)
    await delete_account(db, uid)
    assert (await barrier(db, uid)).state == 'active'

    first = await run_sweep()
    assert (first['promoted'], first['empty']) == (1, 1)
    state, epoch, operation_id, watermark = await barrier(db, uid)
    assert (state, epoch, watermark) == ('deleting', 1, None) and operation_id is not None
    assert (await run_sweep())['closed'] == 1
    assert (await barrier(db, uid)).state == 'deleted'
    assert await events(db, uid) == ['orphan_barrier_promoted', 'residual_sweep_empty',
                                     'subject_deleted']
    # scripts/verify_privacy_barriers.py: no non-deleted barrier without a profile remains.
    assert await db.scalar(text("""
        SELECT count(*) FROM privacy_subject_barriers b
        WHERE b.state <> 'deleted' AND NOT EXISTS (SELECT 1 FROM profiles p WHERE p.id = b.user_id)
    """)) == 0


async def test_live_accounts_and_unfinished_deletions_are_left_alone(db, caplog):
    live = await signup(db)
    stuck = await signup(db)
    stuck_operation = await begin(db, stuck)
    await db.execute(text("UPDATE privacy_subject_barriers SET updated_at = now() - interval '2 hours' "
                          'WHERE user_id = :u'), {'u': stuck})
    fresh = await signup(db)
    await begin(db, fresh)
    profile_only_gone = await signup(db)  # the account itself still exists
    await db.execute(text('DELETE FROM profiles WHERE id = :u'), {'u': profile_only_gone})
    for uid in (live, stuck, profile_only_gone):
        await add_vector(db, uid)
    before = {uid: tuple(await barrier(db, uid)) for uid in (live, stuck, fresh, profile_only_gone)}

    with caplog.at_level(logging.WARNING, logger='moly-worker'):
        detail = await run_sweep()

    assert detail == {'promoted': 0, 'subjects': 0, 'vectors_deleted': 0, 'swept': 0,
                      'empty': 0, 'closed': 0, 'skipped': 0, 'stuck': 1}
    assert {uid: tuple(await barrier(db, uid)) for uid in before} == before
    assert [await vectors(db, uid) for uid in (live, stuck, profile_only_gone)] == [1, 1, 1]
    assert (await barrier(db, stuck)).operation_id == stuck_operation
    assert any('1시간 넘게 삭제 중' in r.getMessage() for r in caplog.records)


async def test_abort_reopens_only_a_live_account_and_restores_the_memory_epoch(db):
    uid = await signup(db)
    await db.execute(text('UPDATE privacy_subject_barriers SET epoch = 3 WHERE user_id = :u'), {'u': uid})
    await db.execute(text("""
        INSERT INTO memory_pipeline_states
          (user_id, mode, bootstrap_status, historical_upper_turn_seq, source_through_turn_seq,
           privacy_epoch)
        VALUES (:u, 'v2', 'ready', 0, 0, 3)
    """), {'u': uid})
    operation_id = await begin(db, uid)
    assert (await barrier(db, uid)).epoch == 4

    assert await abort(db, uid, uuid.uuid4()) is False  # another attempt's operation
    assert (await barrier(db, uid)).state == 'deleting'
    assert await abort(db, uid, operation_id) is True
    assert tuple(await barrier(db, uid))[:3] == ('active', 3, None)
    assert await events(db, uid) == ['serving_blocked_and_redacted', 'deletion_aborted']
    # Memory jobs carry the pipeline's epoch; a mismatch would cancel every one of them.
    state = await privacy.load_barrier(db, uid)
    assert privacy.authorize_job(state, job_type='mem0_ingest', payload_epoch=3).allowed

    never_chatted = await signup(db)
    await abort(db, never_chatted, await begin(db, never_chatted))
    assert tuple(await barrier(db, never_chatted))[:3] == ('active', 0, None)

    no_profile = await signup(db)  # the account exists; only its profile is missing
    no_profile_operation = await begin(db, no_profile)
    await db.execute(text('DELETE FROM profiles WHERE id = :u'), {'u': no_profile})
    assert await abort(db, no_profile, no_profile_operation) is True

    gone = await signup(db)
    gone_operation = await begin(db, gone)
    await delete_account(db, gone)
    assert await abort(db, gone, gone_operation) is False  # the sweep owns it now
    assert (await barrier(db, gone)).state == 'deleting'


async def test_abort_without_a_memory_pipeline_returns_to_the_enrollment_epoch(db):
    """Enrollment starts the pipeline at epoch 0, so a retried then aborted deletion must
    leave the barrier at 0 too, or every memory job after enrollment is cancelled."""
    uid = await signup(db)
    await begin(db, uid)
    operation_id = await begin(db, uid)  # the app retried DELETE /me
    assert (await barrier(db, uid)).epoch == 2
    assert await abort(db, uid, operation_id) is True
    assert tuple(await barrier(db, uid))[:3] == ('active', 0, None)

    assert await memory_pipeline.enroll(db, uid)
    pipeline_epoch = await db.scalar(text(
        'SELECT privacy_epoch FROM memory_pipeline_states WHERE user_id = :u'), {'u': uid})
    state = await privacy.load_barrier(db, uid)
    assert privacy.authorize_job(state, job_type='mem0_ingest', payload_epoch=pipeline_epoch).allowed


async def test_fence_starts_a_new_epoch_and_covers_a_missing_barrier(db):
    uid = await signup(db)
    first = await begin(db, uid)
    second = await begin(db, uid)  # a repeated request starts another attempt
    assert tuple(await barrier(db, uid))[:3] == ('deleting', 2, second)
    assert await abort(db, uid, first) is False

    missing = await signup(db)
    await db.execute(text('DELETE FROM privacy_subject_barriers WHERE user_id = :u'), {'u': missing})
    operation_id = await begin(db, missing)
    assert tuple(await barrier(db, missing)) == ('deleting', 0, operation_id, 0)


async def test_fence_functions_are_callable_by_the_service_role_only(db):
    for name in ('begin_subject_deletion', 'abort_subject_deletion'):
        signature = f'public.{name}(uuid,uuid)'
        assert [await db.scalar(text('SELECT has_function_privilege(:r, :f, :p)'),
                                {'r': role, 'f': signature, 'p': 'EXECUTE'})
                for role in ('anon', 'authenticated', 'service_role')] == [False, False, True]
        assert await db.scalar(text(
            'SELECT prosecdef AND proconfig = ARRAY[\'search_path=""\'] FROM pg_proc '
            'WHERE oid = to_regprocedure(:f)'), {'f': signature})

    uid = await signup(db)
    await db.execute(text('SET LOCAL ROLE service_role'))
    try:
        operation_id = await begin(db, uid)
    finally:
        await db.execute(text('RESET ROLE'))
    assert (await barrier(db, uid)).operation_id == operation_id

    other = await signup(db)
    async with db.begin_nested():
        await db.execute(text('SET LOCAL ROLE anon'))
        with pytest.raises(Exception, match='permission denied'):
            async with db.begin_nested():
                await begin(db, other)
        await db.execute(text('RESET ROLE'))
    assert (await barrier(db, other)).state == 'active'
