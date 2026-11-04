"""Owned synthetic dev rows; every global promotion transaction is rolled back.

Opt in separately because the concurrency fixture commits ONLY its own synthetic
setup so two sessions can see it. It creates no runnable jobs or provider data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import checkpoint as cp, checkpoint_repo as repo, context_retention as retention
from app.services import interaction_contract, retention_release as storage
from app.services import retention_release_promotion as promotion
from app.services.retention_preparation_context import Binding, use_binding
from db.envfile import assert_dev_target, load_conn
from tests.integration.test_provided_memory_dev import seed_memory, CANONICAL


_CONTROL_KEYS = ('context_retention_active_preparation', 'context_retention_v2_enabled',
                 'crisis_guard_enabled', 'crisis_guard_policy_version',
                 'crisis_guard_timeout_s', 'crisis_guard_history_messages')


async def controls(session):
    return dict((await session.execute(text('SELECT key,value FROM app_config WHERE key=ANY(:keys)'),
                                      {'keys': list(_CONTROL_KEYS)})).all())


async def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


@pytest_asyncio.fixture
async def online_promotion_db(monkeypatch):
    if os.environ.get('MOLY_ONLINE_PROMOTION_TEST_ENV') != 'dev':
        pytest.skip('MOLY_ONLINE_PROMOTION_TEST_ENV=dev required (owned committed setup)')
    path = os.environ.get('MOLY_CHECKPOINT_ENV_FILE')
    if not path:
        pytest.skip('explicit MOLY_CHECKPOINT_ENV_FILE required')
    dsn = load_conn(path)
    assert_dev_target(path, dsn)
    engine = create_async_engine(make_url(dsn).set(drivername='postgresql+asyncpg'),
        echo=False, hide_parameters=True, pool_size=3, max_overflow=0,
        connect_args={'statement_cache_size': 0, 'prepared_statement_cache_size': 0,
                      'command_timeout': 10})
    maker = async_sessionmaker(engine, expire_on_commit=False)
    uid, identity = uuid.uuid4(), uuid.uuid4()
    base_users = promotion._USERS
    initialized = False
    journal = None
    try:
        async with maker() as s:
            original_controls = await controls(s)
            if original_controls.get('context_retention_active_preparation') is not None:
                pytest.skip('first-transition test requires no already-active dev preparation')
            if await s.scalar(text("SELECT EXISTS(SELECT 1 FROM retention_preparations WHERE state='active')")):
                pytest.skip('first-transition test requires no already-active dev preparation')
            directory = Path('/private/tmp/moly-online-promotion-dev-20261104')
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
            journal = directory / f'owned-{uuid.uuid4().hex}.json'
            fd = os.open(journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'schema': 'owned-online-promotion-dev-v1',
                    'project_ref': 'wywzjslvxwttxkecbyis', 'user_id': str(uid),
                    'preparation_id': str(identity)}, stream)
            await s.execute(text('INSERT INTO auth.users(id,created_at) VALUES(:u,now())'), {'u': uid})
            initialized = True
            await s.execute(text("INSERT INTO profiles(id,language,timezone) VALUES(:u,'en','UTC') "
                "ON CONFLICT(id) DO UPDATE SET language='en',timezone='UTC'"), {'u': uid})
            await s.execute(text('INSERT INTO chat_contexts(user_id) VALUES(:u) ON CONFLICT DO NOTHING'), {'u': uid})
            await s.execute(text("INSERT INTO privacy_subject_barriers(user_id,state,epoch) VALUES(:u,'active',0) "
                "ON CONFLICT(user_id) DO UPDATE SET state='active',epoch=0"), {'u': uid})
            rid, mid = await seed_memory(s, uid)
            await s.execute(text("INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position) "
                "VALUES(:u,'moly','normal','Synthetic response',CURRENT_DATE,1,2)"), {'u': uid})
            await s.execute(text('UPDATE chat_contexts SET last_committed_turn_seq=1 WHERE user_id=:u'), {'u': uid})
            await s.execute(text("INSERT INTO mem0_ingest_candidates(user_id,turn_seq,candidate_hash,schema_version,"
                "extractor_version,normalizer_version,provider_memory_id,candidate_text,status) "
                "SELECT user_id,source_turn_seq,content_hash,'mem0-candidate-v1','synthetic','mem0-normalizer-v1',"
                "provider_memory_id,:body,'committed' FROM mem0_memory_registry WHERE id=:r"),
                {'r': rid, 'body': CANONICAL})
            row = (await s.execute(text(str(base_users).replace('ORDER BY p.id', 'WHERE p.id=:u')), {'u': uid})).mappings().one()
            plan = await promotion.read_user(s, dict(row))
            manifest = promotion.live_manifest([plan], [], project_ref='wywzjslvxwttxkecbyis', code_sha='a'*40)
            await s.execute(text("INSERT INTO retention_preparations(id,state,code_sha,manifest_json) "
                "VALUES(:p,'building',:sha,CAST(:m AS jsonb))"),
                {'p': identity, 'sha': 'a'*40, 'm': json.dumps(manifest)})
            await promotion.enroll_source_revisions(s, identity, manifest)
            # An old public row proves the OFF reader is unchanged by preparation.
            with use_binding(None):
                old = await repo.insert(s, user_id=uid, through_message_id=plan.through,
                    summary='Synthetic legacy sentinel', source_hash='b'*64, expected_generation=0)
            sources = [cp.SourceMessage(m['id'],m['sender'],m['kind'],m['content'],m['created_at']) for m in plan.messages]
            raw_contract = (await s.execute(text('SELECT id,sender,content,activity_date FROM messages '
                "WHERE user_id=:u AND kind='normal' ORDER BY id"), {'u': uid})).all()
            state = retention.empty_state()
            state['sequence'] = 1
            proof = cp.completed_progress_proof(previous=None, messages=sources, language='en', state=state)
            with use_binding(Binding(identity, True)):
                await storage.store_contract(s, user_id=uid, generation=0, locale='en', items=[], rendered='',
                    digest=interaction_contract.document_hash('[]'), through=plan.through,
                    source_rows=raw_contract, after=0)
                staged = await repo.insert(s, user_id=uid, through_message_id=plan.through,
                    summary=retention.render_state(state), source_hash=cp.source_hash(previous=None,
                        messages=sources, version=cp.RETENTION_SUMMARIZER_VERSION, preparation_id=identity),
                    expected_generation=0, version=cp.RETENTION_SUMMARIZER_VERSION, state_json=state,
                    validation_json={'processing_complete': True,'source_message_ids':[m.id for m in sources],
                                     'completed_proof': proof}, coverage_from=sources[0].id)
            await require(staged is not None, 'synthetic_prepared_checkpoint_insert_failed')
            # No ready job is ever committed; the old development consumer has
            # nothing from this fixture that it can claim.
            await require(await s.scalar(text('SELECT count(*) FROM async_jobs WHERE user_id=:u'), {'u': uid}) == 0,
                          'fixture_must_commit_no_jobs')
            await s.commit()
        # Only this generated UUID is substituted into the test-only population.
        # The production manager itself remains whole-population.
        scoped = text(str(base_users).replace('ORDER BY p.id', f"WHERE p.id='{uid}'::uuid ORDER BY p.id"))
        monkeypatch.setattr(promotion, '_USERS', scoped)
        yield SimpleFixture(maker, uid, identity, mid, old, staged, original_controls)
    except pytest.skip.Exception:
        raise
    except Exception as exc:
        raise pytest.fail.Exception(f'owned_dev_setup_failed:{type(exc).__name__}', pytrace=False) from None
    finally:
        if initialized:
            async with maker() as s:
                # Cleanup only our generated user/preparation, no registry sweep.
                await s.execute(text('DELETE FROM retention_prepared_contracts WHERE preparation_id=:p'), {'p': identity})
                await s.execute(text('DELETE FROM conversation_checkpoints WHERE preparation_id=:p'), {'p': identity})
                await s.execute(text('DELETE FROM auth.users WHERE id=:u'), {'u': uid})
                await s.execute(text('DELETE FROM retention_preparations WHERE id=:p'), {'p': identity})
                await s.commit()
            async with maker() as s:
                remaining = await s.scalar(text('SELECT (SELECT count(*) FROM auth.users WHERE id=:u)+'
                    '(SELECT count(*) FROM profiles WHERE id=:u)+(SELECT count(*) FROM retention_preparations WHERE id=:p)'),
                    {'u': uid, 'p': identity})
                await require(remaining == 0, 'owned_dev_cleanup_incomplete')
                await require(await controls(s) == original_controls, 'global_controls_changed_after_cleanup')
            if journal is not None:
                journal.unlink(missing_ok=True)
        await engine.dispose()


class SimpleFixture:
    def __init__(self, maker, uid, identity, mid, old, staged, original_controls):
        self.maker, self.uid, self.identity, self.mid = maker, uid, identity, mid
        self.old, self.staged, self.original_controls = old, staged, original_controls

    async def audit(self):
        async with self.maker() as s:
            await promotion.seal_preparation(s, self.identity)
            await s.commit()  # Only private completion metadata, never the global pointer.
        async with self.maker() as s:
            await s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            token = await promotion.audit_preparation(s, self.identity)
            await s.rollback()
            return token

    async def unchanged(self):
        async with self.maker() as s:
            await require(await controls(s) == self.original_controls, 'global_controls_not_rolled_back')
            await require(await s.scalar(text('SELECT state FROM retention_preparations WHERE id=:p'),
                          {'p': self.identity}) == 'building', 'registry_not_rolled_back')
            await require(await s.scalar(text('SELECT publish_state FROM conversation_checkpoints WHERE id=:c'),
                          {'c': self.staged}) == 'ready', 'checkpoint_not_rolled_back')


async def prepare_activation(session, token):
    from app.services.retention_activation_db import prepare_envelope
    await session.execute(text('SELECT 1'))
    return await prepare_envelope(session, token)


async def activate_prepared(session, token):
    prepared = await prepare_activation(session, token)
    return await promotion.activate(session, token, deadline=time.monotonic()+1, prepared=prepared)


class ActivationDiagnostic(Exception):
    def __init__(self, kind, sqlstate, stage):
        self.kind, self.sqlstate, self.stage = kind, sqlstate, stage


def sql_stage(statement):
    sql = str(statement)
    if 'WITH checkpoint_publication AS' in sql:
        return 'publication'
    if 'locked_0' in sql:
        return 'row_snapshot'
    if 'SELECT user_id,id,sender' in sql:
        return 'tail'
    if 'jsonb_to_recordset' in sql:
        return 'processing'
    if 'UPDATE async_jobs' in sql:
        return 'job_cancel'
    if 'UPDATE conversation_checkpoints' in sql:
        return 'checkpoint_publish'
    if 'UPDATE retention_prepared_contracts' in sql:
        return 'contract_publish'
    if 'UPDATE retention_preparations' in sql:
        return 'registry_publish'
    if 'app_config' in sql:
        return 'control_write' if 'INSERT INTO' in sql else 'controls'
    if 'FROM profiles p' in sql:
        return 'user_metadata'
    if 'retention_preparations' in sql:
        return 'registry'
    if 'pg_advisory' in sql:
        return 'advisory'
    return 'setup'


async def activate_traced(session, token, *, label):
    steps = []
    class SessionTrace:
        def in_transaction(self):
            return session.in_transaction()

        async def connection(self):
            return await session.connection()

        async def call(self, method, sql, params=None):
            entry = {'stage': sql_stage(sql), 'completed': False}
            steps.append(entry)
            started = time.monotonic()
            try:
                result = await getattr(session, method)(sql, params)
                entry['completed'] = True
                return result
            finally:
                entry['milliseconds'] = round((time.monotonic()-started)*1000, 2)
        async def execute(self, sql, params=None):
            return await self.call('execute', sql, params)
        async def scalar(self, sql, params=None):
            return await self.call('scalar', sql, params)
    trace = SessionTrace()
    setup_started = time.monotonic()
    prepared = await prepare_activation(trace, token)
    setup_ms = round((time.monotonic()-setup_started)*1000, 2)
    steps.clear()
    result, failure = None, None
    started = time.monotonic()
    try:
        result = await promotion.activate(trace, token, deadline=started+1, prepared=prepared)
    except Exception as exc:
        original = getattr(exc, 'orig', None)
        state = getattr(original, 'sqlstate', None)
        state = state if isinstance(state, str) and len(state) == 5 and state.isalnum() else 'unknown'
        failure = ActivationDiagnostic(type(exc).__name__, state, steps[-1]['stage'] if steps else 'setup')
    directory = Path('/private/tmp/moly-online-promotion-dev-20261104')
    target = directory / 'activation-traces.json'
    previous = json.loads(target.read_text()) if target.exists() else []
    previous.append({'case': label, 'success': failure is None, 'setup_ms': setup_ms,
        'milliseconds': round((time.monotonic()-started)*1000, 2), 'steps': steps,
        'error_type': failure.kind if failure else None,
        'sqlstate': failure.sqlstate if failure else None})
    fd = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(previous, stream)
    if failure is not None:
        raise failure
    return result


async def checked(case):
    try:
        await case()
    except Exception as exc:
        # Driver/provider bodies and identifiers must never enter test output.
        if isinstance(exc, ActivationDiagnostic):
            raise pytest.fail.Exception(f'promotion_case_failed:{exc.kind}:{exc.sqlstate}:{exc.stage}', pytrace=False) from None
        original = getattr(exc, 'orig', None)
        state = getattr(original, 'sqlstate', None)
        state = state if isinstance(state, str) and len(state) == 5 and state.isalnum() else 'unknown'
        query = getattr(exc, 'statement', '') or ''
        stage = ('row_snapshot' if 'locked_0' in query else 'tail' if 'SELECT user_id,id,sender' in query
                 else 'processing' if 'jsonb_to_recordset' in query else 'controls' if 'app_config' in query
                 else 'other')
        raise pytest.fail.Exception(f'promotion_case_failed:{type(exc).__name__}:{state}:{stage}', pytrace=False) from None


async def test_actual_atomic_activation_rollback_and_off_reader(online_promotion_db):
    f = online_promotion_db
    async def case():
        token = await f.audit()
        async with f.maker() as s:
            with use_binding(None):
                await require((await repo.load_latest(s, f.uid, retention_enabled=False)).id == f.old,
                              'legacy_reader_changed_before_activation')
                await require(await repo.load_latest(s, f.uid, retention_enabled=True) is None,
                              'private_stage_leaked_before_activation')
            await s.rollback()
            await activate_prepared(s, token)
            await require(await s.scalar(text('SELECT value FROM app_config WHERE key=:k'),
                          {'k': 'context_retention_active_preparation'}) == str(f.identity), 'new_pointer_not_atomic')
            with use_binding(Binding(f.identity, False)):
                await require((await repo.load_latest(s, f.uid, retention_enabled=True)).id == f.staged,
                              'new_projection_not_readable')
            with use_binding(None):
                await require((await repo.load_latest(s, f.uid, retention_enabled=False)).id == f.old,
                              'pinned_legacy_reader_changed')
            await s.rollback()  # GLOBAL ACTIVE STATE IS NEVER COMMITTED.
        await f.unchanged()
    await checked(case)


async def test_actual_append_after_audit_is_preserved_and_can_activate(online_promotion_db):
    f = online_promotion_db
    async def case():
        token = await f.audit()
        async with f.maker() as writer:
            await writer.execute(text("INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position) "
                "VALUES(:u,'user','normal','Synthetic appended source',CURRENT_DATE,2,1),"
                "(:u,'moly','normal','Synthetic appended response',CURRENT_DATE,2,2)"), {'u': f.uid})
            await writer.execute(text('UPDATE chat_contexts SET last_committed_turn_seq=2 WHERE user_id=:u'), {'u': f.uid})
            await writer.commit()
        async with f.maker() as s:
            await activate_prepared(s, token)
            await require(await s.scalar(text('SELECT count(*) FROM messages WHERE user_id=:u AND turn_seq=2'),
                          {'u': f.uid}) == 2, 'appended_raw_was_lost')
            await s.rollback()
        await f.unchanged()
    await checked(case)


async def test_actual_prefix_edit_after_audit_rejects_before_publish(online_promotion_db):
    f = online_promotion_db
    async def case():
        token = await f.audit()
        async with f.maker() as writer:
            await writer.execute(text("UPDATE messages SET content=content||' Synthetic edit.' WHERE id=:m"), {'m': f.mid})
            await writer.commit()
        rejected = False
        async with f.maker() as s:
            try:
                await activate_prepared(s, token)
            except promotion.Blocked:
                rejected = True
            finally:
                await s.rollback()
        await require(rejected, 'changed_prefix_was_not_rejected')
        await f.unchanged()
    await checked(case)


@pytest.mark.parametrize('locked', ['control', 'registry'])
async def test_actual_share_lock_conflict_is_bounded_and_rolls_back(online_promotion_db, locked):
    f = online_promotion_db
    async def case():
        token = await f.audit()
        async with f.maker() as holder, f.maker() as s:
            if locked == 'registry':
                await holder.execute(text('SELECT id FROM retention_preparations WHERE id=:p FOR SHARE'), {'p': f.identity})
            else:
                await holder.execute(text("SELECT key FROM app_config WHERE key='context_retention_active_preparation' FOR SHARE"))
            prepared = await prepare_activation(s, token)
            started, rejected = time.monotonic(), False
            try:
                await promotion.activate(s, token, deadline=started+1, prepared=prepared)
            except Exception:
                rejected = True
            finally:
                await s.rollback()
                await holder.rollback()
            elapsed = time.monotonic()-started
            await require(rejected and elapsed < 1.5, 'contended_promotion_not_bounded')
        await f.unchanged()
    await checked(case)


async def test_actual_abort_cancels_only_uncommitted_owned_ready_jobs(online_promotion_db):
    f = online_promotion_db
    async def case():
        async with f.maker() as s:
            job = await s.scalar(text("INSERT INTO async_jobs(queue,job_type,user_id,dedup_key,payload,max_attempts) "
                "VALUES('content','conversation_checkpoint',:u,:key,CAST(:payload AS jsonb),3) RETURNING id"),
                {'u': f.uid, 'key': str(uuid.uuid4()), 'payload': json.dumps({
                    'retention_preparation_id': str(f.identity), 'retention_preparation_building': True})})
            # The ready row is visible only to this transaction, never a worker.
            await promotion.abort(s, f.identity)
            row = (await s.execute(text('SELECT state,lease_owner,lease_token,lease_until,finished_at,payload_expires_at '
                'FROM async_jobs WHERE id=:id'), {'id': job})).one()
            good = row[0] == 'cancelled' and all(value is None for value in row[1:4]) and all(value is not None for value in row[4:])
            await require(good, 'owned_abort_terminal_receipt_invalid')
            await s.rollback()
        await f.unchanged()
    await checked(case)


async def test_actual_public_nonnull_coverage_can_coexist_with_promoted_stage(online_promotion_db):
    f = online_promotion_db
    async def case():
        async with f.maker() as writer:
            through = await writer.scalar(text('SELECT through_message_id FROM conversation_checkpoints WHERE id=:c'),
                                          {'c': f.old})
            await writer.execute(text('UPDATE conversation_checkpoints SET coverage_from_message_id=:first,'
                'coverage_through_message_id=:last,segment_through_message_id=:last WHERE id=:c'),
                {'first': f.mid, 'last': through, 'c': f.old})
            await writer.commit()  # Owned synthetic legacy row only.
        token = await f.audit()
        async with f.maker() as s:
            await activate_traced(s, token, label='nonnull_legacy_coverage')
            rows = (await s.execute(text("SELECT preparation_id FROM conversation_checkpoints WHERE user_id=:u "
                "AND coverage_through_message_id=:through AND (publish_state='published' "
                "OR (preparation_id=:p AND publish_state='ready' AND validation_json->'activation_sealed'='true'::jsonb))"),
                {'u': f.uid, 'through': through, 'p': f.identity})).all()
            ok = len(rows) == 2 and sum(row[0] is None for row in rows) == 1
            await require(ok, 'prepared_and_legacy_same_boundary_conflicted')
            with use_binding(None):
                legacy = await repo.load_latest(s, f.uid, retention_enabled=False)
                await require(legacy.id == f.old, 'nonnull_coverage_changed_legacy_projection')
            await s.rollback()
        await f.unchanged()
    await checked(case)
