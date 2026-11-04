"""No database/provider calls: finite first-promotion and live-prefix contracts."""
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import retention_release_promotion as manager
from app.services.retention_preparation_context import Binding, use_binding
from tests.test_guard_context_release_prepare import Session, fixture, Result

SHA = 'a'*40


def manifest(uid):
    return {'format': manager.FORMAT, 'project_ref': 'synthetic', 'code_sha': SHA,
            'users': {str(uid): {'through': 2, 'fingerprint': 'proof', 'generation': 0, 'epoch': 0}},
            'excluded': []}


def token(uid, identity):
    m = manifest(uid)
    return manager.AuditToken(identity, manager.digest(m), SHA, time.monotonic(), (), {}, {},
                              manager.public_report(m, preparation_id=identity, ready=True), m, '1', {})


def test_public_report_never_serializes_internal_subjects_or_raw():
    uid, identity = uuid.uuid4(), uuid.uuid4()
    value = manifest(uid)
    value['excluded'] = [[str(uuid.uuid4()), 'deleted', 0, 0]]
    report = manager.public_report(value, preparation_id=identity)
    assert str(uid) not in json.dumps(report)
    assert report['preparation_id'] == str(identity)
    assert report['excluded_authorization'] == {'deleted': 1}
    assert manager.FORMAT != 'guard-context-release-v1'


async def test_prefix_query_binds_end_before_order_and_retains_source_proof():
    user, messages, row, source = fixture()
    session = Session(user, messages, [row], [source])
    await manager.read_user(session, user, through_id=2)
    source_query = next(q for q, _ in session.queries if 'LEFT JOIN messages m' in q)
    assert source_query.index('s.source_message_id<=:through') < source_query.index('ORDER BY s.registry_id')


async def test_prefix_null_fill_is_idempotent_but_existing_raw_change_is_not():
    user, messages, row, source = fixture()
    session = Session(user, messages, [row], [source])
    original = manager.prefix_record(await manager.read_user(session, user, through_id=2))
    row['canonical_text'] = 'verified source'
    assert manager.prefix_record(await manager.read_user(session, user, through_id=2)) == original
    messages[-1]['content'] += ' changed'
    assert manager.prefix_record(await manager.read_user(session, user, through_id=2)) != original


async def test_prepare_requires_explicit_building_permission_before_writes():
    user, messages, row, source = fixture()
    session = Session(user, messages, [row], [source])
    plan = await manager.read_user(session, user)
    session.queries.clear()
    with pytest.raises(manager.Blocked, match='building_binding_required'):
        await manager.prepare_user(session, plan, preparation_id=uuid.uuid4())
    assert session.queries == [] and row['canonical_text'] is None


def test_source_hash_is_release_namespaced_without_changing_null_compatibility():
    from app.services import checkpoint
    values = dict(previous=None, messages=[], version=checkpoint.RETENTION_SUMMARIZER_VERSION)
    assert manager.scoped_source_hash(**values) == checkpoint.source_hash(**values)
    identity = uuid.uuid4()
    with use_binding(Binding(identity, True)):
        assert manager.scoped_source_hash(**values) == checkpoint.source_hash(**values, preparation_id=identity)
        assert manager.scoped_source_hash(**values) != checkpoint.source_hash(**values)


async def test_new_uuid_cannot_reset_unchanged_terminal_failed_subject():
    uid = uuid.uuid4()
    plan = manager.UserPlan({'user_id': uid, 'memory_generation': 0, 'barrier_epoch': 0}, [], [], {}, 2, 'proof')
    m = manager.live_manifest([plan], [], project_ref='synthetic', code_sha=SHA)
    session = SimpleNamespace(scalar=AsyncMock(return_value=False))
    session.execute = AsyncMock(side_effect=[Result([]), Result([]), Result([(m, uid)])])
    with pytest.raises(manager.Blocked, match='unchanged_terminal_failure'):
        await manager.new_preparation(session, plans=[plan], excluded=[], project_ref='synthetic', code_sha=SHA)
    assert not any('INSERT INTO retention_preparations' in str(c.args[0]) for c in session.execute.await_args_list)


@pytest.mark.parametrize('state', ['building', 'prepared', 'aborted'])
async def test_abort_preserves_history_and_cancels_only_unlocked_ready_private_jobs(monkeypatch, state):
    uid, identity = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(manager, 'registry', AsyncMock(return_value={
        'id': identity, 'state': state, 'manifest_json': manifest(uid)}))
    session = SimpleNamespace(execute=AsyncMock(return_value=Result([])))
    await manager.abort(session, identity)
    sql = '\n'.join(str(c.args[0]) for c in session.execute.await_args_list)
    assert 'SKIP LOCKED' in sql and "WHERE state='ready'" in sql
    cancel = next(str(c.args[0]) for c in session.execute.await_args_list if 'UPDATE async_jobs' in str(c.args[0]))
    assert 'updated_at' not in cancel and 'finished_at=now()' in cancel
    assert 'lease_owner=NULL' in cancel and 'payload_expires_at=COALESCE' in cancel
    assert 'DELETE FROM async_jobs' not in sql and 'state=\'running\'' not in sql
    assert 'UPDATE messages' not in sql and 'user_interaction_contracts' not in sql


async def test_active_preparation_is_never_aborted(monkeypatch):
    monkeypatch.setattr(manager, 'registry', AsyncMock(return_value={'state': 'active'}))
    session = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(manager.Blocked, match='active_preparation_cannot'):
        await manager.abort(session, uuid.uuid4())
    assert session.execute.await_count == 1


@pytest.mark.parametrize('fault', ['manifest', 'active', 'rows', 'processing'])
async def test_activation_rejects_before_any_public_write(monkeypatch, fault):
    from app.services import retention_activation_db
    uid, identity = uuid.uuid4(), uuid.uuid4()
    t = token(uid, identity)
    receipt = dict(registry={'state':'prepared'}, controls=[], new_users=[], tail=[],
                   verified_revisions=fault != 'processing', revision_rows=[])
    envelope = AsyncMock(return_value=receipt)
    if fault == 'rows':
        receipt['dirty_subjects'] = [str(uid)]
    if fault in {'manifest','active'}:
        envelope.side_effect = manager.Blocked('preparation_changed_since_audit')
    monkeypatch.setattr(retention_activation_db, 'validation_envelope', envelope)
    session = SimpleNamespace(scalar=AsyncMock(return_value=True), execute=AsyncMock())
    with pytest.raises(manager.Blocked):
        await manager.activate(session, t, deadline=time.monotonic()+1, prepared=object())
    session.execute.assert_not_awaited()


async def test_atomic_promotion_uses_tail_only_and_set_proof(monkeypatch):
    from app.services import retention_activation_db
    uid, identity = uuid.uuid4(), uuid.uuid4()
    t = token(uid, identity)
    envelope = AsyncMock(return_value=dict(registry={'state':'prepared'}, controls=[],
        new_users=[],tail=[],verified_revisions=True,revision_rows=[]))
    monkeypatch.setattr(retention_activation_db, 'validation_envelope', envelope)
    session = SimpleNamespace(scalar=AsyncMock(return_value=True), execute=AsyncMock(
        return_value=Result([{'preparations_activated':1,'controls_written':6}])))
    prepared = object()
    await manager.activate(session, t, deadline=time.monotonic()+1, prepared=prepared)
    envelope.assert_awaited_once_with(session, t, prepared)
    session.execute.assert_awaited_once()
    statement, params = session.execute.await_args.args
    sql = str(statement)
    assert 'WITH job_cancellation AS' in sql and 'jsonb_each' in sql
    assert 'UPDATE conversation_checkpoints' not in sql
    assert 'UPDATE retention_prepared_contracts' not in sql
    assert 'crisis_guard_history_messages' in params['values']
    assert 'FOR UPDATE SKIP LOCKED' in sql
    assert 'CROSS JOIN preparation_activation' in sql
    assert '(SELECT count(*) FROM job_cancellation)>=0' in sql
    assert 'UPDATE user_interaction_contracts' not in sql
    assert not hasattr(session, 'commit')


async def test_expired_token_executes_no_sql():
    t = token(uuid.uuid4(), uuid.uuid4())
    t = manager.AuditToken(t.preparation_id, t.manifest_hash, t.code_sha,
        time.monotonic()-61, (), {}, {}, t.report, t.manifest, t.registry_version, t.source_revisions)
    session = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(manager.Blocked, match='audit_token_expired'):
        await manager.activate(session, t, deadline=time.monotonic()+1, prepared=object())
    session.execute.assert_not_awaited()


async def test_final_proof_is_one_set_query_not_per_user():
    uid, identity = uuid.uuid4(), uuid.uuid4()
    session = SimpleNamespace(scalar=AsyncMock(return_value=True))
    assert await manager.processing_set_complete(session, identity, manifest(uid))
    session.scalar.assert_awaited_once()
    sql = str(session.scalar.await_args.args[0])
    assert 'jsonb_to_recordset' in sql and 'retention_prepared_contracts' in sql
    assert "status='planned'" in sql and 'consolidated_through_turn_seq' in sql


async def test_row_version_transfer_is_fixed_size_but_locks_the_real_rows():
    uid = uuid.uuid4()
    plan = manager.UserPlan({'user_id': uid}, [], [], {}, 2, '')
    session = SimpleNamespace(execute=AsyncMock(return_value=Result([{'scope': scope, 'count': 12345, 'signature': 'a'*64} for scope in ('profiles','pipelines','contexts','barriers','messages','memories','sources','checkpoints','contracts')])))
    result = await manager.row_versions(session, uuid.uuid4(), [plan], lock=True)
    assert len(result) == 9
    assert all(v == {'count': 12345, 'signature': 'a'*64} for v in result.values())
    for call in session.execute.await_args_list:
        sql = str(call.args[0])
        assert 'WITH locked_0 AS MATERIALIZED' in sql
        assert 'FOR UPDATE NOWAIT' in sql
        assert 'sha256' in sql and 'count(*)' in sql
        assert 'ORDER BY to_jsonb' not in sql and 'row_to_json(locked_' in sql
        assert 'ORDER BY locked_6.registry_id,locked_6.source_message_id' in sql
        assert 'FOR UPDATE OF s NOWAIT' in sql
        assert 'JOIN locked_5 r ON r.id=s.registry_id AND r.owner_id=s.user_id' in sql
        assert 'r.user_id AS owner_id' in sql
        assert "concat_ws('|',locked_4.id::text,locked_4.version::text)" in sql
        assert "concat_ws('|',locked_6.registry_id::text,locked_6.source_message_id::text,locked_6.evidence_start_utf8::text,locked_6.evidence_end_utf8::text,locked_6.version::text)" in sql
        assert 'row_to_json(locked_0)::text' in sql
        assert 'row_to_json(locked_4)' not in sql
        assert "c.status IN ('ready','superseded')" in sql
        assert "s.source_message_id=m.id AND r.semantic_status='active'" in sql


async def test_abort_keeps_failed_subject_receipt_after_job_payload_scrub(monkeypatch):
    uid, identity = uuid.uuid4(), uuid.uuid4()
    original = manifest(uid)
    monkeypatch.setattr(manager, 'registry', AsyncMock(return_value={
        'id': identity, 'state': 'building', 'manifest_json': original}))
    replies = [Result([]), Result([uid]), Result([]), Result([])]
    session = SimpleNamespace(execute=AsyncMock(side_effect=replies))
    await manager.abort(session, identity)
    write = next(c for c in session.execute.await_args_list if 'manifest_json=CAST' in str(c.args[0]))
    stored = json.loads(write.args[1]['manifest'])
    assert stored['terminal_failures'][str(uid)] == original['users'][str(uid)]
    assert 'content' not in json.dumps(stored)


def test_tail_budget_retains_all_known_uncovered_normal_content():
    from app.services import context_retention
    context = {'system': ['persona'], 'facts': 'required fact', 'contract': 'required contract', 'through': 2}
    messages = [dict(id=3, sender='user', kind='normal', content='uncovered'*10000)]
    with pytest.raises(context_retention.RetentionError, match='context_input_budget'):
        manager.check_tail_budget(context, messages)
    # An excluded safety turn is not silently substituted into ordinary input.
    manager.check_tail_budget(context, [{**messages[0], 'kind': 'safety'}])


def test_live_start_template_allows_new_append_but_not_code_environment_or_old_format():
    from scripts import prepare_guard_context_release as cli
    uid = uuid.uuid4()
    approved = manager.public_report(manifest(uid))
    checksum = manager.digest(approved)
    cli.approve_live_template(approved, checksum, project_ref='synthetic', code_sha=SHA)
    # A start snapshot may contain new complete turns/new eligible subjects.
    # It is privately sealed; activation still independently verifies all users.
    for key, value in [('format', 'guard-context-release-v1'), ('project_ref', 'other'),
                       ('code_sha', 'b'*40), ('blockers', {'source_proof_invalid': 1})]:
        changed = {**approved, key: value}
        with pytest.raises(manager.Blocked):
            cli.approve_live_template(changed, manager.digest(changed), project_ref='synthetic', code_sha=SHA)
    with pytest.raises(manager.Blocked):
        cli.approve_live_template(approved, 'forged', project_ref='synthetic', code_sha=SHA)


async def test_prefix_identity_ignores_live_derived_transitions_but_not_source_language_or_epoch():
    user, messages, row, source = fixture()
    plan = await manager.read_user(Session(user, messages, [row], [source]), user)
    record = manager.prefix_record(plan)
    # Consolidation can supersede the active memory, or change bookkeeping, while
    # the raw prefix and prior checkpoint draft remain exactly the same input.
    derived = copy.deepcopy(plan)
    derived.memories = []
    derived.canonical = {}
    derived.fingerprint = 'a different currently active derived snapshot'
    derived.user['repair_generation'] += 1
    assert manager.prefix_record(derived) == record
    for field, value in [('memory_generation', 1), ('barrier_epoch', 1),
                         ('language', 'ja'), ('timezone', 'Asia/Tokyo')]:
        changed = copy.deepcopy(plan)
        changed.user[field] = value
        assert manager.prefix_record(changed) != record
    changed = copy.deepcopy(plan)
    changed.messages[-1]['kind'] = 'safety'
    assert manager.prefix_record(changed) != record
    changed = copy.deepcopy(plan)
    changed.messages[-1]['row_version'] = 'bookkeeping-only'
    assert manager.prefix_record(changed) == record


async def test_dead_family_suffix_has_no_accidental_sqlalchemy_literal_binds():
    session = SimpleNamespace(execute=AsyncMock(return_value=Result([])), scalar=AsyncMock(side_effect=[False,0]))
    await manager.new_preparation(session, plans=[], excluded=[], project_ref='synthetic', code_sha=SHA)
    for call in session.execute.await_args_list:
        assert set(call.args[0]._bindparams) <= {'id', 'sha', 'manifest', 'targets', 'next_value', 'ids'}


async def test_later_complete_normal_append_preserves_the_sealed_prefix():
    user, messages, row, source = fixture()
    class PrefixSession(Session):
        async def execute(self, sql, params=None):
            if 'FROM messages WHERE user_id' in str(sql) and params and 'through' in params:
                return Result([m for m in self.messages if m['id'] <= params['through']])
            return await super().execute(sql, params)
    session = PrefixSession(user, messages, [row], [source])
    original = manager.prefix_record(await manager.read_user(session, user, through_id=2))
    messages.extend([{**messages[0], 'id': 3, 'turn_seq': 2}, {**messages[1], 'id': 4, 'turn_seq': 2}])
    user['last_committed_turn_seq'] = 2
    assert manager.prefix_record(await manager.read_user(session, user, through_id=2)) == original
    assert manager.prefix_record(await manager.read_user(session, user)) != original


async def test_audit_rejects_read_committed_before_any_source_query():
    session = SimpleNamespace(scalar=AsyncMock(return_value='read committed'), execute=AsyncMock())
    with pytest.raises(manager.Blocked, match='consistent_audit_snapshot_required'):
        await manager.audit_preparation(session, uuid.uuid4())
    session.execute.assert_not_awaited()


def test_empty_subject_initial_generation_zero_is_not_a_deleted_history_reset():
    uid = uuid.uuid4()
    empty = manager.UserPlan({'user_id': uid, 'memory_generation': None, 'barrier_epoch': 0,
        'language': 'ko', 'timezone': 'Asia/Seoul'}, [], [], {}, 0, '')
    initialized = copy.deepcopy(empty)
    initialized.user['memory_generation'] = 0
    assert manager.prefix_record(empty) == manager.prefix_record(initialized)
    initialized.user['memory_generation'] = 1
    assert manager.prefix_record(empty) != manager.prefix_record(initialized)


async def test_audit_also_requires_canonical_for_new_tail_derived_memories(monkeypatch):
    user, messages, row, source = fixture()
    plan = await manager.read_user(Session(user, messages, [row], [source]), user)
    identity = uuid.uuid4()
    monkeypatch.setattr(manager, 'registry', AsyncMock(return_value={'state': 'building',
        'code_sha': SHA, 'manifest_json': manifest(user['user_id'])}))
    monkeypatch.setattr(manager, 'collect', AsyncMock(return_value=([plan], {}, 1, [])))
    session = SimpleNamespace(scalar=AsyncMock(side_effect=['repeatable read','on']))
    with pytest.raises(manager.Blocked, match='canonical_backfill_incomplete'):
        await manager.audit_preparation(session, identity)


async def test_excluded_only_prefix_requires_no_prepared_contract_head(monkeypatch):
    from app.services import checkpoint, checkpoint_repo, context_retention, contract_repo, retention_release
    user, messages, _, _ = fixture()
    for message in messages:
        message['kind'] = 'safety'
    session = Session(user, messages, [], [])
    plan = await manager.read_user(session, user)
    cid = uuid.uuid4()
    sources = [checkpoint.SourceMessage(m['id'],m['sender'],m['kind'],m['content'],m['created_at']) for m in messages]
    record = dict(id=cid, through_message_id=2, previous_checkpoint_id=None, summary='empty progress',
        version=checkpoint.RETENTION_SUMMARIZER_VERSION, state_json={},
        validation_json={'processing_complete': True, 'source_message_ids': [1,2], 'completed_proof': {}},
        coverage_from_message_id=1, coverage_through_message_id=2, segment_through_message_id=2,
        source_hash=checkpoint.source_hash(previous=None, messages=sources, version=checkpoint.RETENTION_SUMMARIZER_VERSION))
    value = checkpoint.Checkpoint(cid, 2, record['summary'], record['version'], record['source_hash'])
    monkeypatch.setattr(checkpoint_repo,'load_latest',AsyncMock(return_value=value))
    monkeypatch.setattr(checkpoint_repo,'coverage_for_checkpoint',AsyncMock(return_value=SimpleNamespace(through_message_id=2)))
    monkeypatch.setattr(context_retention,'load_required_facts',AsyncMock(return_value=''))
    monkeypatch.setattr(context_retention,'validate_progress_sources',AsyncMock())
    monkeypatch.setattr(contract_repo,'published_text',AsyncMock(return_value=''))
    proof = AsyncMock(return_value=True)
    monkeypatch.setattr(retention_release,'validate_contract_chain',proof)
    monkeypatch.setattr(checkpoint,'validate_completed_progress',lambda **kwargs: record['summary'])
    session.execute = AsyncMock(return_value=Result([record]))
    assert await manager.audit_user(session, plan)
    assert proof.await_args.kwargs['through_id'] == 0


@pytest.mark.parametrize('acquired', [False, None, 1, 'true'])
async def test_activation_lock_busy_or_invalid_never_enters_publication(acquired):
    session = SimpleNamespace(scalar=AsyncMock(return_value=acquired), execute=AsyncMock())
    with pytest.raises(manager.Blocked, match='activation_busy'):
        await manager.activate(session, token(uuid.uuid4(), uuid.uuid4()), deadline=time.monotonic()+1, prepared=object())
    session.scalar.assert_awaited_once()
    session.execute.assert_not_awaited()


async def test_activation_lock_configures_then_tries_same_key_in_one_roundtrip():
    session = SimpleNamespace(scalar=AsyncMock(return_value=True))
    await manager.acquire_activation_lock(session)
    session.scalar.assert_awaited_once()
    sql = str(session.scalar.await_args.args[0])
    assert 'configured AS MATERIALIZED' in sql
    assert "set_config('lock_timeout','150ms',true)" in sql
    assert "set_config('work_mem','16MB',true)" in sql
    assert 'pg_try_advisory_xact_lock(743021149)' in sql
    assert sql.index('set_config') < sql.index('pg_try_advisory_xact_lock')
    assert 'FROM configured' in sql
    assert not hasattr(session, 'commit')


async def test_activation_deadline_also_covers_initial_setup_query():
    import asyncio
    async def pending(*args):
        await asyncio.Event().wait()
    session = SimpleNamespace(scalar=AsyncMock(side_effect=pending), execute=AsyncMock())
    with pytest.raises(TimeoutError):
        await manager.activate(session, token(uuid.uuid4(), uuid.uuid4()), deadline=time.monotonic()+0.01, prepared=object())
    session.execute.assert_not_awaited()


async def test_activation_fence_excludes_dml_without_blocking_normal_reads():
    session = SimpleNamespace(execute=AsyncMock())
    await manager.fence_activation_sources(session)
    session.execute.assert_awaited_once()
    sql = str(session.execute.await_args.args[0])
    assert 'IN SHARE MODE NOWAIT' in sql
    for table in ('profiles', 'memory_pipeline_states', 'chat_contexts',
                  'privacy_subject_barriers', 'messages', 'mem0_memory_registry',
                  'mem0_memory_sources', 'mem0_ingest_candidates',
                  'conversation_checkpoints', 'retention_prepared_contracts'):
        assert 'public.' + table in sql
    assert 'ACCESS EXCLUSIVE' not in sql and 'async_jobs' not in sql


async def test_unlocked_source_signature_preserves_owner_filter_with_set_join():
    scopes = ('profiles','pipelines','contexts','barriers','messages','memories','sources','checkpoints','contracts')
    session = SimpleNamespace(execute=AsyncMock(return_value=Result([
        {'scope':scope,'count':1,'signature':'a'*64} for scope in scopes])))
    await manager.row_versions(session, uuid.uuid4(), [], lock=False)
    sql = str(session.execute.await_args.args[0])
    assert 'joined_sources AS MATERIALIZED' in sql
    assert 'FULL JOIN locked_5 r ON r.id=s.registry_id AND r.owner_id=s.user_id' in sql
    assert 'matching_owner IS NOT NULL AND registry_id IS NOT NULL' in sql
    assert 'FOR UPDATE' not in sql


@pytest.mark.parametrize('change', [None, 'version', 'code', 'format'])
async def test_compact_registry_binds_exact_audited_snapshot(change):
    t = token(uuid.uuid4(), uuid.uuid4())
    row = dict(id=t.preparation_id,state='building',code_sha=t.code_sha,
        format=manager.FORMAT,bound_code_sha=t.code_sha,row_version=t.registry_version,active_id=None)
    if change:
        row[{'version':'row_version','code':'code_sha','format':'format'}[change]] = 'changed'
    session = SimpleNamespace(execute=AsyncMock(return_value=Result([row])))
    if change:
        with pytest.raises(manager.Blocked):
            await manager.lock_audited_registry(session, t)
    else:
        result = await manager.lock_audited_registry(session, t)
        assert result['manifest_json'] is t.manifest
    sql = str(session.execute.await_args.args[0])
    assert 'xmin::text AS row_version' in sql and 'FOR UPDATE NOWAIT' in sql
    assert 'code_sha,manifest_json,' not in sql


async def test_audit_requires_readonly_even_when_isolation_is_repeatable_read():
    session = SimpleNamespace(scalar=AsyncMock(side_effect=['repeatable read','off']),execute=AsyncMock())
    with pytest.raises(manager.Blocked, match='readonly_audit_snapshot_required'):
        await manager.audit_preparation(session, uuid.uuid4())
    session.execute.assert_not_awaited()


@pytest.mark.parametrize('complete', [True, False])
async def test_activation_snapshot_preserves_both_builders_in_one_query(complete):
    identity, uid = uuid.uuid4(), uuid.uuid4()
    m = manifest(uid)
    plan = manager.UserPlan({'user_id': uid}, [], [], {}, 2, '')
    signatures, signature_params = manager.row_versions_statement(identity, [plan])
    completion, completion_params = manager.processing_statement(identity, m)
    scopes = ('profiles','pipelines','contexts','barriers','messages','memories','sources','checkpoints','contracts')
    rows = [{'scope': scope, 'count': 1, 'signature': 'a'*64,
             'processing_complete': complete} for scope in scopes]
    session = SimpleNamespace(execute=AsyncMock(return_value=Result(rows)))
    versions, actual = await manager.activation_snapshot(session, identity, [plan], m)
    assert actual is complete and set(versions) == set(scopes)
    session.execute.assert_awaited_once()
    sql, params = session.execute.await_args.args
    assert signatures in str(sql) and completion in str(sql)
    assert 'signatures AS MATERIALIZED' in str(sql)
    assert 'completion(value) AS MATERIALIZED' in str(sql)
    assert params == {**signature_params, **completion_params}
    assert set(json.loads(params['targets'])[0]) == {'user_id','through','generation','epoch'}
    assert 'fingerprint' not in params['targets']


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'null', 'mixed'])
async def test_activation_snapshot_rejects_incomplete_or_ambiguous_result(fault):
    scopes = ('profiles','pipelines','contexts','barriers','messages','memories','sources','checkpoints','contracts')
    rows = [{'scope': scope, 'count': 0, 'signature': 'a'*64,
             'processing_complete': True} for scope in scopes]
    if fault == 'missing':
        rows.pop()
    elif fault == 'duplicate':
        rows.append(dict(rows[0]))
    else:
        rows[-1]['processing_complete'] = None if fault == 'null' else False
    session = SimpleNamespace(execute=AsyncMock(return_value=Result(rows)))
    with pytest.raises(manager.Blocked, match='snapshot_metadata_unavailable'):
        await manager.activation_snapshot(session, uuid.uuid4(), [], {'users': {}})


@pytest.mark.parametrize('state', ['building', 'prepared', 'active', None])
async def test_seal_only_complete_private_ready_artifacts_and_preserve_public_state(state):
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[state, 3]))
    identity = uuid.uuid4()
    if state in {'building', 'prepared'}:
        assert await manager.seal_preparation(session, identity) == 3
        sql = str(session.scalar.await_args.args[0])
        assert "publish_state='ready'" in sql and 'preparation_id=:id' in sql
        assert "jsonb_typeof(validation_json->'completed_proof')='object'" in sql
        assert "OR (validation_json->'reverified'='true'::jsonb" in sql
        assert "jsonb_typeof(validation_json->'page_manifests')='array'" in sql
        assert "validation_json->'page_manifests'<>'[]'::jsonb" in sql
        assert "validation_json->'processing_complete'='true'::jsonb" in sql
        assert "validation_json->'activation_sealed' IS DISTINCT FROM 'true'::jsonb" in sql
        assert 'SET publish_state' not in sql and 'UPDATE retention_prepared_contracts' not in sql
        assert not hasattr(session, 'commit')
    else:
        with pytest.raises(manager.Blocked, match='preparation_not_building'):
            await manager.seal_preparation(session, identity)
        assert session.scalar.await_count == 1


async def test_seal_statement_has_only_preparation_id_binding():
    session = SimpleNamespace(scalar=AsyncMock(side_effect=['building',1]))
    await manager.seal_preparation(session, uuid.uuid4())
    statement = session.scalar.await_args.args[0]
    assert set(statement.compile().params) == {'id'}


async def test_revision_registration_bumps_only_private_start_fence_under_nowait():
    session = SimpleNamespace(scalar=AsyncMock(return_value=2),execute=AsyncMock())
    await manager.fence_revision_registration(session)
    assert 'FOR UPDATE NOWAIT' in str(session.scalar.await_args.args[0])
    assert session.execute.await_args.args[1] == {'next_value':3}
    assert 'context_retention_revision_fence' in str(session.execute.await_args.args[0])
