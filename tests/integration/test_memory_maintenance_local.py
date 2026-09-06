"""Maintenance executes against the real canonical schema, always rolled back."""
import hashlib
import json
import uuid

import asyncpg
import pytest

from db.maintenance import MaintenanceBlocked, enqueue_recovery, locked_subject
from scripts import reextract_memories, resume_reextract, repair_memory_backlog
from tests.integration.test_schema_bootstrap_local import connection as _schema_connection, signup

connection = _schema_connection


async def pipeline(conn, *, caught_up=True, epoch=4):
    uid, _ = await signup(conn)
    await conn.execute('UPDATE privacy_subject_barriers SET epoch=$2 WHERE user_id=$1', uid, epoch)
    await conn.execute('''INSERT INTO memory_pipeline_states
        (user_id,mode,bootstrap_status,source_through_turn_seq,ingest_through_turn_seq,
         consolidated_through_turn_seq,privacy_epoch)
        VALUES ($1,'v2','ready',5,$2,$2,$3)''', uid, 5 if caught_up else 0, epoch)
    await conn.execute('''INSERT INTO messages(user_id,sender,content,activity_date,turn_seq,
        turn_position,created_at) VALUES ($1,'user','local test',DATE '2026-01-01',5,1,
                                        TIMESTAMPTZ '2026-01-01 00:00:00Z')''', uid)
    return uid


async def registry(conn, uid, *, status='active', mark='mem0-classifier-v2'):
    provider_id = uuid.uuid4()
    rid = await conn.fetchval('''INSERT INTO mem0_memory_registry
        (user_id,provider,collection_version,provider_memory_id,source_turn_seq,
         content_hash,schema_version,semantic_status,classification_version)
        VALUES ($1,'mem0','moly-memories-v2',$2,5,'fixture','v1',$3,$4) RETURNING id''',
        uid, provider_id, status, mark)
    await conn.execute('''INSERT INTO vecs.moly_memories_v2(id,vec,metadata)
        VALUES ($1,array_fill(0::real,ARRAY[1536])::public.vector,jsonb_build_object('user_id',$2::text))''',
        str(provider_id), str(uid))
    return rid, provider_id


async def test_reextract_uses_current_key_priority_epoch_and_preserves_originals(connection):
    uid = await pipeline(connection)
    rid, provider_id = await registry(connection, uid)
    await reextract_memories.reextract(connection, limit=1, idle_min=10, apply=True, min_turns=1)
    row = await connection.fetchrow("SELECT * FROM async_jobs WHERE user_id=$1 AND job_type='mem0_ingest'", uid)
    assert row['dedup_key'] == f'mem0:{uid}:c0:v1:g1'
    assert row['queue'] == 'memory' and row['priority'] == 500 and row['max_attempts'] == 8
    payload = json.loads(row['payload'])
    assert payload == {'turn_seq': 5, 'privacy_epoch': 4}
    wire = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    assert row['payload_hash'] == hashlib.sha256(wire.encode()).hexdigest()
    assert row['payload_schema_version'] == 'job-payload-v1'
    assert await connection.fetchval('SELECT classification_version FROM mem0_memory_registry WHERE id=$1', rid) == 'pre-reextract-active'
    assert await connection.fetchval('SELECT count(*) FROM vecs.moly_memories_v2 WHERE id=$1', str(provider_id)) == 1
    assert await connection.fetchval('SELECT count(*) FROM messages WHERE user_id=$1', uid) == 1
    assert await connection.fetchval('SELECT ingest_through_turn_seq FROM memory_pipeline_states WHERE user_id=$1', uid) == 0


@pytest.mark.parametrize('state', ['deleting', 'deleted', 'missing', 'epoch_mismatch', 'running'])
async def test_maintenance_blocks_unsafe_subject_before_any_cursor_change(connection, state):
    uid = await pipeline(connection)
    if state == 'missing':
        await connection.execute('DELETE FROM privacy_subject_barriers WHERE user_id=$1', uid)
    elif state == 'epoch_mismatch':
        await connection.execute('UPDATE privacy_subject_barriers SET epoch=9 WHERE user_id=$1', uid)
    elif state == 'running':
        async with locked_subject(connection, uid):
            jid = await enqueue_recovery(connection, job_type='mem0_ingest', user_id=uid,
                                         dedup_key='local-running', payload={'turn_seq': 5, 'privacy_epoch': 4})
        await connection.execute("UPDATE async_jobs SET state='running',lease_owner='local-worker',"
                                 "lease_token=$2,lease_until=now()+interval '1 minute' WHERE id=$1", jid, uuid.uuid4())
    else:
        await connection.execute('UPDATE privacy_subject_barriers SET state=$2,operation_id=$3 WHERE user_id=$1', uid, state, uuid.uuid4())
    with pytest.raises(MaintenanceBlocked):
        async with locked_subject(connection, uid):
            pytest.fail('unsafe subject admitted')
    assert await connection.fetchval('SELECT ingest_through_turn_seq FROM memory_pipeline_states WHERE user_id=$1', uid) == 5


async def test_resume_replays_dead_with_lineage_and_is_not_a_timestamp_dedup_bypass(connection):
    uid = await pipeline(connection, caught_up=False)
    async with locked_subject(connection, uid):
        original = await enqueue_recovery(connection, job_type='mem0_ingest', user_id=uid,
                                           dedup_key=f'mem0:{uid}:c0:v1', payload={'turn_seq': 5, 'privacy_epoch': 4}, priority=500)
    await connection.execute("UPDATE async_jobs SET state='dead',finished_at=now() WHERE id=$1", original)
    await resume_reextract.resume(connection, limit=1, idle_min=10, apply=True)
    rows = await connection.fetch('SELECT id,state,replay_of,payload FROM async_jobs WHERE user_id=$1 ORDER BY created_at,id', uid)
    assert len(rows) == 2
    assert next(r for r in rows if r['id'] == original)['state'] == 'dead'
    replay = next(r for r in rows if r['replay_of'] == original)
    assert replay['state'] == 'ready'
    assert json.loads(replay['payload'])['privacy_epoch'] == 4
    await resume_reextract.resume(connection, limit=1, idle_min=10, apply=True)
    assert await connection.fetchval('SELECT count(*) FROM async_jobs WHERE user_id=$1', uid) == 2


async def test_repair_pending_uses_memory_queue_and_current_epoch(connection):
    uid = await pipeline(connection)
    await registry(connection, uid, status='pending')
    assert await repair_memory_backlog.repair(connection, apply=True, limit=1) == (1, 0)
    row = await connection.fetchrow('SELECT queue,payload,max_attempts FROM async_jobs WHERE user_id=$1', uid)
    assert row['queue'] == 'memory' and row['max_attempts'] == 8
    assert json.loads(row['payload']) == {'turn_seq': 5, 'privacy_epoch': 4}


async def test_rollback_restores_both_statuses_without_deleting_vectors(connection):
    uid = await pipeline(connection)
    active, _ = await registry(connection, uid, status='superseded', mark='pre-reextract-active')
    ambiguous, _ = await registry(connection, uid, status='superseded', mark='pre-reextract-ambiguous')
    new, _ = await registry(connection, uid)
    await reextract_memories.rollback(connection, apply=True, limit=1)
    rows = {r['id']: r['semantic_status'] for r in await connection.fetch('SELECT id,semantic_status FROM mem0_memory_registry WHERE user_id=$1', uid)}
    assert rows == {active: 'active', ambiguous: 'ambiguous', new: 'superseded'}
    assert await connection.fetchval("SELECT count(*) FROM vecs.moly_memories_v2 WHERE metadata->>'user_id'=$1", str(uid)) == 3


async def test_rollback_refuses_missing_original_vector_atomically(connection):
    uid = await pipeline(connection)
    rid, provider_id = await registry(connection, uid, status='superseded', mark='pre-reextract-active')
    await connection.execute('DELETE FROM vecs.moly_memories_v2 WHERE id=$1', str(provider_id))
    with pytest.raises(MaintenanceBlocked, match='original_vectors_unavailable'):
        await reextract_memories.rollback(connection, apply=True, limit=1)
    assert await connection.fetchval('SELECT semantic_status FROM mem0_memory_registry WHERE id=$1', rid) == 'superseded'


async def test_operator_previews_do_not_change_pipeline_or_jobs(connection):
    uid = await pipeline(connection)
    await registry(connection, uid)
    before = dict(await connection.fetchrow('SELECT * FROM memory_pipeline_states WHERE user_id=$1', uid))
    await reextract_memories.reextract(connection, limit=1, idle_min=10, apply=False, min_turns=1)
    await resume_reextract.resume(connection, limit=1, idle_min=10, apply=False)
    await repair_memory_backlog.repair(connection, apply=False)
    assert dict(await connection.fetchrow('SELECT * FROM memory_pipeline_states WHERE user_id=$1', uid)) == before
    assert await connection.fetchval('SELECT count(*) FROM async_jobs WHERE user_id=$1', uid) == 0


@pytest.mark.parametrize('condition', ['expired', 'redacted', 'replayed'])
async def test_recovery_refuses_unusable_dead_payload_or_existing_replay(connection, condition):
    uid = await pipeline(connection)
    async with locked_subject(connection, uid):
        original = await enqueue_recovery(connection, job_type='mem0_ingest', user_id=uid,
            dedup_key='local-terminal', payload={'turn_seq': 5, 'privacy_epoch': 4})
    await connection.execute("UPDATE async_jobs SET state='dead',finished_at=now() WHERE id=$1", original)
    if condition == 'expired':
        await connection.execute("UPDATE async_jobs SET payload_expires_at=now()-interval '1 minute' WHERE id=$1", original)
    elif condition == 'redacted':
        await connection.execute("UPDATE async_jobs SET payload_redacted_at=now() WHERE id=$1", original)
    else:
        async with locked_subject(connection, uid):
            await enqueue_recovery(connection, job_type='mem0_ingest', user_id=uid,
                dedup_key='local-terminal', payload={'turn_seq': 5, 'privacy_epoch': 4})
    count = await connection.fetchval('SELECT count(*) FROM async_jobs WHERE user_id=$1', uid)
    with pytest.raises(MaintenanceBlocked):
        async with locked_subject(connection, uid):
            await enqueue_recovery(connection, job_type='mem0_ingest', user_id=uid,
                dedup_key='local-terminal', payload={'turn_seq': 5, 'privacy_epoch': 4})
    assert await connection.fetchval('SELECT count(*) FROM async_jobs WHERE user_id=$1', uid) == count


async def test_enqueue_failure_rolls_back_hiding_cursor_and_generation(connection, monkeypatch):
    uid = await pipeline(connection)
    rid, _ = await registry(connection, uid)
    before = dict(await connection.fetchrow('SELECT * FROM memory_pipeline_states WHERE user_id=$1', uid))
    async def fail(*args, **kwargs):
        raise MaintenanceBlocked('injected_enqueue_failure')
    monkeypatch.setattr(reextract_memories, 'enqueue_recovery', fail)
    with pytest.raises(MaintenanceBlocked, match='injected_enqueue_failure'):
        await reextract_memories.reextract(connection, limit=1, idle_min=10, apply=True, min_turns=1)
    assert dict(await connection.fetchrow('SELECT * FROM memory_pipeline_states WHERE user_id=$1', uid)) == before
    assert await connection.fetchval('SELECT semantic_status FROM mem0_memory_registry WHERE id=$1', rid) == 'active'
    assert await connection.fetchval('SELECT count(*) FROM async_jobs WHERE user_id=$1', uid) == 0


async def test_unknown_current_handler_replay_preserves_history_and_filters_retired_types(connection):
    from scripts.replay_dead_memory_jobs import replay
    uid = await pipeline(connection, caught_up=False)
    async with locked_subject(connection, uid):
        original = await enqueue_recovery(connection, job_type='mem0_ingest', user_id=uid,
            dedup_key=f'mem0:{uid}:c0:v1', payload={'turn_seq': 5, 'privacy_epoch': 4})
    await connection.execute("UPDATE async_jobs SET state='dead',finished_at=now(),"
                             "last_error_code='unknown_job_type' WHERE id=$1", original)
    await connection.execute("INSERT INTO async_jobs(queue,job_type,user_id,dedup_key,payload,state,last_error_code,max_attempts) "
                             "VALUES ('memory','memory_extract',$1,'retired','{}','dead','unknown_job_type',8)", uid)
    assert await replay(connection, execute=True, limit=10) == 1
    assert await replay(connection, execute=True, limit=10) == 0
    assert await connection.fetchval('SELECT state FROM async_jobs WHERE id=$1', original) == 'dead'
    assert await connection.fetchval('SELECT count(*) FROM async_jobs WHERE replay_of=$1', original) == 1
    assert await connection.fetchval("SELECT count(*) FROM async_jobs WHERE job_type='memory_extract' AND user_id=$1", uid) == 1


async def test_lock_contention_blocks_repair_and_queued_claim():
    import os
    from db.schema_contract import require_scratch
    dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('MOLY_SCHEMA_TEST_DSN required')
    require_scratch(dsn)
    first, second = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
    uid = None
    try:
        # Committed local fixture is necessary for two independent sessions.
        uid = await pipeline(first)
        async with locked_subject(first, uid):
            jid = await enqueue_recovery(first, job_type='mem0_ingest', user_id=uid,
                dedup_key='local-contention-'+str(uid), payload={'turn_seq': 5, 'privacy_epoch': 4})
        async with locked_subject(first, uid):
            with pytest.raises(asyncpg.LockNotAvailableError):
                async with locked_subject(second, uid):
                    pytest.fail('contended subject admitted')
            async with second.transaction():
                assert await second.fetchval("SELECT id FROM async_jobs WHERE id=$1 "
                                              "FOR UPDATE SKIP LOCKED", jid) is None
        async with locked_subject(second, uid):
            pass  # Lock was released after the first operator transaction.
    finally:
        if uid:
            await first.execute('DELETE FROM async_jobs WHERE user_id=$1', uid)
            await first.execute('DELETE FROM auth.users WHERE id=$1', uid)
            await first.execute('DELETE FROM privacy_subject_barriers WHERE user_id=$1', uid)
        await first.close()
        await second.close()


async def test_orphan_repair_preserves_live_vectors_registered_and_recent_plans(connection):
    uid = await pipeline(connection)
    _, registered = await registry(connection, uid)
    vector_only = uuid.uuid4()
    await connection.execute("INSERT INTO vecs.moly_memories_v2(id,vec,metadata) "
        "VALUES($1,array_fill(0::real,ARRAY[1536])::public.vector,jsonb_build_object('user_id',$2::text))",
        str(vector_only), str(uid))
    cases = [('orphan', uuid.uuid4(), True), ('registered', registered, True),
             ('vector', vector_only, True), ('recent', uuid.uuid4(), False)]
    ids = {}
    for label, provider, old in cases:
        ids[label] = await connection.fetchval('''INSERT INTO mem0_ingest_candidates
          (user_id,turn_seq,candidate_hash,schema_version,extractor_version,normalizer_version,
           provider_memory_id,candidate_text,created_at)
          VALUES($1,5,$2,'v1','local','local',$3,'local test',
                 now()-CASE WHEN $4 THEN interval '1 hour' ELSE interval '0' END) RETURNING id''',
          uid, label, provider, old)
    assert await repair_memory_backlog.repair(connection, apply=True, limit=10) == (0, 1)
    states = {key: await connection.fetchval('SELECT status FROM mem0_ingest_candidates WHERE id=$1', val)
              for key, val in ids.items()}
    assert states == {'orphan': 'dead', 'registered': 'planned', 'vector': 'planned', 'recent': 'planned'}
