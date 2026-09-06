"""SQLAlchemy operator paths execute the real SQL against a disposable local DB."""
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import NullPool

from db.maintenance import MaintenanceBlocked
from db.schema_contract import require_scratch
from scripts import enter_shadow_cohort, verify_shadow_entry
from tests.integration.test_memory_maintenance_local import pipeline


@pytest_asyncio.fixture
async def subject():
    dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('MOLY_SCHEMA_TEST_DSN required')
    require_scratch(dsn)
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'), poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session:
            await session.execute(text('SELECT 1'))
            sqlconn = await session.connection()
            raw = (await sqlconn.get_raw_connection()).driver_connection
            uid = await pipeline(raw)
            await raw.execute("UPDATE memory_pipeline_states SET mode='legacy',bootstrap_status='legacy',"
                              "historical_upper_turn_seq=NULL,source_through_turn_seq=0,"
                              "ingest_through_turn_seq=0,consolidated_through_turn_seq=0 WHERE user_id=$1", uid)
            yield session, raw, uid
            await session.rollback()
    finally:
        await engine.dispose()


async def test_enrollment_starts_at_actual_first_turn_and_current_privacy_epoch(subject):
    session, conn, uid = subject
    assert 'turn=5' in await enter_shadow_cohort._one(session, uid, apply=True)
    row = await conn.fetchrow('SELECT * FROM memory_pipeline_states WHERE user_id=$1', uid)
    assert row['privacy_epoch'] == 4 and row['bootstrap_status'] == 'ready'
    assert row['source_through_turn_seq'] == 5
    assert await conn.fetchval("SELECT (payload->>'privacy_epoch')::int FROM async_jobs WHERE user_id=$1", uid) == 4
    assert '건너뜀' in await enter_shadow_cohort._one(session, uid, apply=True)
    assert await conn.fetchval('SELECT count(*) FROM async_jobs WHERE user_id=$1', uid) == 1


async def test_rollback_verifier_accepts_historical_gaps(subject):
    session, _, uid = subject
    assert await verify_shadow_entry.run(session, uid, 5) == []


@pytest.mark.parametrize('invalid', ['epoch', 'deleting', 'missing'])
async def test_enrollment_does_not_revive_a_blocked_subject(subject, invalid):
    session, conn, uid = subject
    if invalid == 'epoch':
        await conn.execute('UPDATE memory_pipeline_states SET privacy_epoch=0 WHERE user_id=$1', uid)
    elif invalid == 'missing':
        await conn.execute('DELETE FROM privacy_subject_barriers WHERE user_id=$1', uid)
    else:
        await conn.execute("UPDATE privacy_subject_barriers SET state='deleting',operation_id=$2 WHERE user_id=$1", uid, uuid.uuid4())
    with pytest.raises(MaintenanceBlocked):
        await enter_shadow_cohort._one(session, uid, apply=True)
    assert await conn.fetchval('SELECT mode FROM memory_pipeline_states WHERE user_id=$1', uid) == 'legacy'
    assert await conn.fetchval('SELECT count(*) FROM async_jobs WHERE user_id=$1', uid) == 0


@pytest.mark.parametrize('changed', ['none', 'epoch', 'source', 'deleted'])
async def test_contract_draft_rechecks_source_and_privacy_before_atomic_write(subject, changed):
    from scripts import backfill_interaction_contracts as backfill
    from app.services import contract_compiler as cc, interaction_contract as ic
    session, conn, uid = subject
    source = (await session.execute(backfill._MESSAGES, {'user_id': uid, 'limit': 301})).all()
    candidate = cc.Candidate(ic.Directive(ic.Kind.ADDRESS, ic.Action.USE,
        ic.Condition.ALWAYS, ic.Polarity.POSITIVE, target_literal='친구'), source[0][0], 'local test')
    if changed == 'epoch':
        await conn.execute('UPDATE privacy_subject_barriers SET epoch=5 WHERE user_id=$1', uid)
    elif changed == 'source':
        await conn.execute("UPDATE messages SET content='changed local test' WHERE user_id=$1", uid)
    elif changed == 'deleted':
        await conn.execute("UPDATE privacy_subject_barriers SET state='deleted',operation_id=$2 WHERE user_id=$1", uid, uuid.uuid4())
    if changed == 'none':
        assert await backfill._persist_draft(session, uid, [candidate], 'en', 4, source) == 1
        assert await conn.fetchval('SELECT status FROM user_interaction_contracts WHERE user_id=$1', uid) == 'draft'
        assert await conn.fetchval('SELECT count(*) FROM user_interaction_contract_items WHERE user_id=$1', uid) == 1
    else:
        with pytest.raises(MaintenanceBlocked):
            await backfill._persist_draft(session, uid, [candidate], 'en', 4, source)
        assert await conn.fetchval('SELECT count(*) FROM user_interaction_contracts WHERE user_id=$1', uid) == 0
