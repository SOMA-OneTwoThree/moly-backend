"""Opt-in, existing development Supabase only; unique synthetic rows are cleaned.

MOLY_DIARY_BACKFILL_TEST_ENV=dev MOLY_DIARY_BACKFILL_ENV_FILE=/absolute/dev/.env pytest ...
No provider calls, global mode changes, production queries, or existing member reads.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import hashlib
import os
from types import SimpleNamespace
import uuid

import asyncpg
import pytest
import pytest_asyncio

from app.services.diary_shared_embeddings import approved_welcome_pairs
from db.envfile import assert_dev_target, load_conn
from scripts import diary_shared_embeddings as tool


A = '[' + ','.join(['1'] + ['0'] * 1535) + ']'
B = '[' + ','.join(['0', '1'] + ['0'] * 1534) + ']'


class DevFixture(SimpleNamespace):
    def __repr__(self):
        return '<isolated development fixture>'


@pytest_asyncio.fixture
async def backfill_db(monkeypatch):
    if os.environ.get('MOLY_DIARY_BACKFILL_TEST_ENV') != 'dev':
        pytest.skip('MOLY_DIARY_BACKFILL_TEST_ENV=dev required')
    env = os.environ.get('MOLY_DIARY_BACKFILL_ENV_FILE')
    if not env or not os.path.isabs(env):
        pytest.fail('explicit absolute development env file required')
    dsn = load_conn(env)
    assert_dev_target(env, dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=15)
    model = 'qa-diary-backfill-' + uuid.uuid4().hex
    # Every tool query is restricted before planning, not merely filtered after
    # reading existing users' content. model contains only our fixed prefix/hex.
    monkeypatch.setattr(tool, '_BASE_FROM', tool._BASE_FROM + f" AND rd.embedding_model='{model}'\n")
    pairs = approved_welcome_pairs()
    welcome = ([x[0] for x in pairs], [x[1] for x in pairs])
    mode = {'value': 'dual'}
    actual_read_mode = tool.read_mode

    async def synthetic_mode(*args, **kwargs):
        return mode['value']

    monkeypatch.setattr(tool, 'read_mode', synthetic_mode)
    users, ments = [], []

    async def user():
        uid = uuid.uuid4()
        await conn.execute('INSERT INTO auth.users(id,created_at) VALUES($1,now())', uid)
        users.append(uid)
        await conn.execute('INSERT INTO profiles(id) VALUES($1) ON CONFLICT DO NOTHING', uid)
        await conn.execute('INSERT INTO chat_contexts(user_id) VALUES($1) ON CONFLICT DO NOTHING', uid)
        await conn.execute("INSERT INTO privacy_subject_barriers(user_id,state,epoch) VALUES($1,'active',0) ON CONFLICT DO NOTHING", uid)
        return uid

    async def ment():
        mid = uuid.uuid4()
        await conn.execute("""INSERT INTO moly_life_ments(id,content,weather,week_start_date,sequence_no)
 VALUES($1,$2,'sunny',$3,1)""", mid, 'Synthetic approved public copy ' + uuid.uuid4().hex,
                           date(4000, 1, 3) + timedelta(weeks=uuid.uuid4().int % 100000))
        ments.append(mid)
        return mid

    async def diary(uid, mid, vector=A, *, day=0):
        did = uuid.uuid4()
        await conn.execute("""INSERT INTO diaries
 (id,user_id,diary_date,activity_date,display_date,source,preset_ment_id,content,weather,kind,published_at)
 SELECT $1,$2,$3,$3,$3,'preset',id,content,'sunny','capi_day',now()-interval '1 day'
 FROM moly_life_ments WHERE id=$4""", did, uid, date(2001, 1, 1) + timedelta(days=day), mid)
        await conn.execute("""INSERT INTO diary_recall_documents
 (user_id,diary_id,search_text,source_hash,embedding,embedding_model,suppression_generation,index_version)
 SELECT user_id,id,trim(content),encode(digest(content,'sha256'),'hex'),$3::vector(1536),$4,0,'diary-recall-v1'
 FROM diaries WHERE user_id=$1 AND id=$2""", uid, did, vector, model)
        return did

    async def plan(action):
        async with conn.transaction(readonly=True, isolation='repeatable_read'):
            await tool.prepare_tx(conn)
            cutoff = await conn.fetchval('SELECT now()')
            rows = await conn.fetch(tool.plan_sql(action), *welcome)
            assert all(r['embedding_model'] == model for r in rows)
            return {'version': 1, 'action': action, 'project': 'wywzjslvxwttxkecbyis',
                    'cutoff': cutoff.isoformat(), 'entries': [dict(r) for r in rows]}

    async def apply(plan, batch=50, max_batches=10):
        return await tool.apply_plan(conn, plan, welcome=welcome, batch=batch, max_batches=max_batches)

    try:
        yield DevFixture(conn=conn, dsn=dsn, env=env, model=model, mode=mode, user=user,
                              ment=ment, diary=diary, plan=plan, apply=apply, welcome=welcome,
                              actual_read_mode=actual_read_mode)
    finally:
        for uid in users:
            await conn.execute('DELETE FROM privacy_ledger_events WHERE user_id=$1', uid)
            await conn.execute('DELETE FROM auth.users WHERE id=$1', uid)
            await conn.execute('DELETE FROM privacy_subject_barriers WHERE user_id=$1', uid)
        for mid in ments:
            await conn.execute('DELETE FROM moly_life_ments WHERE id=$1', mid)
        await conn.execute('DELETE FROM diary_template_embeddings WHERE embedding_model=$1', model)
        assert await conn.fetchval('SELECT count(*) FROM auth.users WHERE id=ANY($1::uuid[])', users) == 0
        assert await conn.fetchval('SELECT count(*) FROM diary_template_embeddings WHERE embedding_model=$1', model) == 0
        await conn.close()


async def test_bounded_backfill_prune_and_old_generation_restore(backfill_db):
    db = backfill_db
    mid = await db.ment()
    ids = []
    for vector in (A, A, A, B):
        uid = await db.user()
        ids.append((uid, await db.diary(uid, mid, vector)))
    db.mode['value'] = 'private'
    planned = await db.plan('link')
    assert len(planned['entries']) == 1 and planned['entries'][0]['candidate_count'] == 3
    changed = 0
    for _ in range(4):
        changed += (await db.apply(planned, batch=1, max_batches=1))['changed']
    assert changed == 3
    assert await db.conn.fetchval('SELECT count(*) FROM diary_recall_documents WHERE embedding_model=$1 '
                                  'AND shared_embedding_id IS NOT NULL', db.model) == 3
    # Existing ready canonical is fixed even if a reviewed candidate uses the other vector.
    altered = {**planned, 'entries': [{**planned['entries'][0],
                'vector_digest': hashlib.sha256((await db.conn.fetchval(
                    'SELECT embedding::text FROM diary_recall_documents WHERE user_id=$1 AND diary_id=$2',
                    *ids[3])).encode()).hexdigest()}]}
    assert (await db.apply(altered))['changed'] == 0
    assert await db.conn.fetchval('SELECT embedding=$2::vector(1536) FROM diary_template_embeddings '
                                  'WHERE embedding_model=$1', db.model, A)
    db.mode['value'] = 'shared'
    assert (await db.apply(await db.plan('prune')))['changed'] == 3
    for uid, _ in ids[:3]:
        await db.conn.execute('UPDATE chat_contexts SET memory_generation=1 WHERE user_id=$1', uid)
    # Rollback must also materialize a scheduled future publication without
    # making that diary visible before its existing publication time.
    await db.conn.execute("UPDATE diaries SET published_at=now()+interval '1 day' WHERE id=$1", ids[0][1])
    db.mode['value'] = 'dual'
    restore = await db.plan('restore')
    assert restore['entries'][0]['candidate_count'] == 3
    assert (await db.apply(restore))['changed'] == 3
    assert await db.conn.fetchval('SELECT published_at>now() FROM diaries WHERE id=$1', ids[0][1])
    assert await db.conn.fetchval('SELECT count(*) FROM diaries WHERE id=$1 AND published_at<=now()', ids[0][1]) == 0
    assert await db.conn.fetchval('SELECT count(*) FROM diary_recall_documents WHERE embedding_model=$1 '
                                  'AND embedding IS NULL', db.model) == 0
    assert await db.conn.fetchval('SELECT embedding=$3::vector(1536) AND shared_embedding_id IS NULL '
                                  'FROM diary_recall_documents WHERE user_id=$1 AND diary_id=$2', *ids[3], B)


async def test_changed_source_and_actual_vector_mismatch_are_preserved(backfill_db):
    db = backfill_db
    mid = await db.ment()
    users = [await db.user(), await db.user()]
    docs = [await db.diary(users[0], mid), await db.diary(users[1], mid)]
    plan = await db.plan('link')
    # Seed/link the first row, then prepare a fresh candidate snapshot for the second.
    assert (await db.apply(plan, batch=1, max_batches=1))['changed'] == 1
    candidate_rows = await db.conn.fetch(tool.candidate_sql('link'), *db.welcome,
                                         *tool.key_values(plan['entries'][0]),
                                         datetime.fromisoformat(plan['cutoff']),
                                         uuid.UUID(int=0), uuid.UUID(int=0), 10)
    assert len(candidate_rows) == 1
    candidate = candidate_rows[0]
    # An unrelated actual vector cannot be linked even under the same approved plan digest.
    await db.conn.execute('UPDATE diary_recall_documents SET embedding=$3::vector(1536) '
                          'WHERE user_id=$1 AND diary_id=$2', candidate['user_id'], candidate['diary_id'], B)
    async with db.conn.transaction():
        assert not await tool.mutate_one(db.conn, 'link', plan['entries'][0], candidate, db.welcome)
    assert await db.conn.fetchval('SELECT shared_embedding_id IS NULL FROM diary_recall_documents '
                                  'WHERE user_id=$1 AND diary_id=$2', candidate['user_id'], candidate['diary_id'])
    # Editing the diary invalidates source provenance even if projection metadata stays unchanged.
    await db.conn.execute("UPDATE diaries SET content='Edited synthetic source' WHERE id=$1", candidate['diary_id'])
    async with db.conn.transaction():
        assert not await tool.mutate_one(db.conn, 'link', plan['entries'][0], candidate, db.welcome)
    assert len(docs) == 2


async def test_epoch_changed_while_waiting_for_user_lock_is_fenced(backfill_db):
    db = backfill_db
    uid = await db.user()
    did = await db.diary(uid, await db.ment())
    plan = await db.plan('link')
    async with db.conn.transaction():
        assert await tool.seed_canonical(db.conn, plan['entries'][0], db.welcome,
                                         datetime.fromisoformat(plan['cutoff']))
    candidate = (await db.conn.fetch(tool.candidate_sql('link'), *db.welcome,
                                     *tool.key_values(plan['entries'][0]),
                                     datetime.fromisoformat(plan['cutoff']),
                                     uuid.UUID(int=0), uuid.UUID(int=0), 1))[0]
    other = await asyncpg.connect(db.dsn, statement_cache_size=0, timeout=10)
    holder = db.conn.transaction()
    await holder.start()
    await db.conn.execute(tool._USER_LOCK, str(uid))

    async def delayed_mutation():
        async with other.transaction():
            await tool.prepare_tx(other)
            return await tool.mutate_one(other, 'link', plan['entries'][0], candidate, db.welcome)

    task = asyncio.create_task(delayed_mutation())
    try:
        await asyncio.sleep(0.1)
        assert not task.done()
        await db.conn.execute('UPDATE privacy_subject_barriers SET epoch=epoch+1 WHERE user_id=$1', uid)
        await holder.commit()
        assert await asyncio.wait_for(task, 5) is False
        assert await db.conn.fetchval('SELECT shared_embedding_id IS NULL FROM diary_recall_documents '
                                      'WHERE user_id=$1 AND diary_id=$2', uid, did)
    finally:
        if not task.done():
            task.cancel()
        if db.conn.is_in_transaction():
            await holder.rollback()
        await other.close()


async def test_plan_and_default_cli_actions_write_nothing(backfill_db, tmp_path):
    db = backfill_db
    uid = await db.user()
    await db.diary(uid, await db.ment())
    assert await db.actual_read_mode(db.conn) in {'private', 'dual', 'shared'}
    output = tmp_path / 'reviewed.json'
    args = SimpleNamespace(env=db.env, commit=False, allow_prod=False, action='plan', output=output,
                           for_action='link', plan=None, expected_plan_sha256=None, batch=50, max_batches=10)
    result = await tool.run(args)
    assert result['mutations'] == 0 and result['source_keys'] == 1
    args.action, args.output, args.plan = 'link', None, output
    args.expected_plan_sha256 = result['plan_sha256']
    assert (await tool.run(args))['mutations'] == 0
    assert await db.conn.fetchval('SELECT count(*) FROM diary_template_embeddings WHERE embedding_model=$1', db.model) == 0
    assert await db.conn.fetchval('SELECT shared_embedding_id IS NULL FROM diary_recall_documents '
                                  'WHERE user_id=$1', uid)


async def test_production_write_is_refused_before_connect(monkeypatch):
    opened = False

    async def forbidden_connect(*args, **kwargs):
        nonlocal opened
        opened = True
        raise AssertionError('must never connect')

    monkeypatch.setattr(tool, 'load_conn', lambda _: 'postgresql://postgres.qkgjlgzsharnilxnkytd:fake@pooler/db')
    monkeypatch.setattr(tool.asyncpg, 'connect', forbidden_connect)
    args = SimpleNamespace(env='.env.prod', commit=True, allow_prod=False)
    with pytest.raises(SystemExit):
        await tool.run(args)
    assert not opened
