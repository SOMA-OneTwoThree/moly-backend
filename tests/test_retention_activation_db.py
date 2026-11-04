"""Server envelope construction and same-transaction driver bridge, no DB."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import retention_activation_db as envelope
from tests.test_retention_release_promotion import token


def test_bindings_are_fixed_and_postgres_casts_are_preserved():
    assert envelope.bind_variables('SELECT :id::uuid,CAST(:ids AS uuid[]),:bounds,:targets') == (
        'SELECT v_id::uuid,CAST(v_ids AS uuid[]),v_bounds,v_targets')
    with pytest.raises(ValueError, match='unsupported_activation_binding'):
        envelope.bind_variables('SELECT :unreviewed')


def test_envelope_uses_builders_exact_scope_and_authorized_raw_order():
    value = token(uuid.uuid4(), uuid.uuid4())
    name = 'activation_receipt_' + uuid.uuid4().hex
    sql = envelope.envelope_statement(value, name)
    assert sql.startswith('DO $activation_')
    assert 'CREATE TEMP TABLE pg_temp.' + name in sql
    assert 'ON COMMIT DROP' in sql and 'CREATE FUNCTION' not in sql
    assert 'COMMIT;' not in sql and 'UPDATE conversation_checkpoints' not in sql
    assert 'pg_try_advisory_xact_lock(743021149)' in sql
    assert "current_setting('transaction_isolation') <> 'read committed'" in sql
    assert sql.index("RAISE EXCEPTION 'activation_read_committed_required'") < sql.index('pg_try_advisory_xact_lock')
    assert 'activation_read_committed_required' in envelope._ERRORS
    assert 'IN SHARE MODE NOWAIT' not in sql
    assert 'retention_preparation_revisions' in sql
    assert 'retention_preparation_revision_events' in sql
    assert 'count(*) AS total' in sql and 'c.revision+COALESCE(e.total,0)' in sql
    assert 'MAX(id)' not in sql and 'max(id)' not in sql
    assert 'WHERE preparation_id=v_id ORDER BY user_id FOR UPDATE NOWAIT' in sql
    assert 'preparation_id=v_id AND user_id=ANY(v_ids)' not in sql
    assert sql.index("RAISE EXCEPTION 'prepared_artifacts_unsealed'") < sql.index('SELECT user_id,id,sender,kind,content')
    assert 'completion(value)' not in sql and 'WITH signatures' not in sql
    assert sql.index("v_revisions IS DISTINCT FROM v_payload->'revisions'") < sql.index('SELECT user_id,id,sender,kind,content')
    assert "'verified_revisions',true,'revision_rows',v_revision_rows" in sql
    assert "'row_versions'" not in sql
    assert 'WHERE user_id=ANY(v_ids)' in sql
    assert 'WHERE user_id=ANY(v_audited)' not in sql
    assert 'EXCEPT SELECT unnest(v_audited)' in sql
    assert ':targets' not in sql and ':bounds' not in sql
    assert "'dirty_subjects',v_dirty" in sql
    assert sql.index("'dirty_subjects',v_dirty") < sql.index('SELECT user_id,id,sender,kind,content')
    assert 'FULL JOIN jsonb_each(v_revisions)' in sql
    assert 'fingerprint' not in sql
    assert str(value.preparation_id) not in sql
    assert value.code_sha not in sql
    assert "current_setting('moly.activation_payload_" in sql


@pytest.mark.parametrize('name', ['receipt', 'activation_receipt_;DROP TABLE profiles', 'activation_receipt_'+'a'*31])
def test_receipt_identifier_is_not_sql_input(name):
    with pytest.raises(ValueError, match='invalid_activation_receipt_name'):
        envelope.envelope_statement(token(uuid.uuid4(), uuid.uuid4()), name)


def test_token_manifest_is_verified_before_payload_construction():
    value = token(uuid.uuid4(), uuid.uuid4())
    value.manifest['code_sha'] = 'b'*40
    with pytest.raises(envelope.promotion.Blocked, match='preparation_changed_since_audit'):
        envelope.envelope_statement(value, 'activation_receipt_' + uuid.uuid4().hex)


def bridge_session(*, warmed=True):
    driver = SimpleNamespace(is_in_transaction=lambda: warmed, execute=AsyncMock())
    transaction = object()
    connection = SimpleNamespace(get_raw_connection=AsyncMock(return_value=SimpleNamespace(driver_connection=driver)),
        sync_connection=SimpleNamespace(get_nested_transaction=lambda: transaction, get_transaction=lambda: None))
    return SimpleNamespace(in_transaction=lambda: True, connection=AsyncMock(return_value=connection),
        execute=AsyncMock(), scalar=AsyncMock(return_value={'registry': {'state': 'prepared'}})), driver


async def test_prepare_only_uploads_parameterized_metadata_without_source_locks():
    value = token(uuid.uuid4(), uuid.uuid4())
    session, driver = bridge_session()
    prepared = await envelope.prepare_envelope(session, value)
    sql, params = session.execute.await_args.args
    assert str(sql) == "SELECT set_config(:payload_key,:payload,true)"
    assert json.loads(params['payload'])['id'] == str(value.preparation_id)
    assert params['payload_key'] == prepared.payload_key
    assert str(value.preparation_id) not in prepared.statement
    assert len(prepared.payload_key) < 64
    assert session.connection.await_count == 1
    driver.execute.assert_not_awaited()
    assert not hasattr(session, 'commit')


async def test_prepared_objects_are_isolated_and_validation_never_uploads_again():
    value = token(uuid.uuid4(), uuid.uuid4())
    session, driver = bridge_session()
    first = await envelope.prepare_envelope(session, value)
    second = await envelope.prepare_envelope(session, value)
    assert first.name != second.name and first.payload_key != second.payload_key
    session.execute.reset_mock()
    assert await envelope.validation_envelope(session, value, first) == {'registry': {'state': 'prepared'}}
    session.execute.assert_not_awaited()
    driver.execute.assert_awaited_once_with(first.statement)
    assert first.name in str(session.scalar.await_args.args[0])


@pytest.mark.parametrize('fault', ['token', 'manifest', 'statement', 'key'])
async def test_prepared_envelope_cannot_cross_tokens_or_be_tampered(fault):
    from dataclasses import replace
    value = token(uuid.uuid4(), uuid.uuid4())
    session, driver = bridge_session()
    prepared = await envelope.prepare_envelope(session, value)
    if fault == 'token':
        value = token(uuid.uuid4(), uuid.uuid4())
    elif fault == 'manifest':
        value.manifest['code_sha'] = 'b'*40
    elif fault == 'statement':
        prepared = replace(prepared, statement='DO invalid')
    else:
        prepared = replace(prepared, payload_key='other')
    with pytest.raises(envelope.promotion.Blocked, match='preparation_changed_since_audit'):
        await envelope.validation_envelope(session, value, prepared)
    driver.execute.assert_not_awaited()


async def test_validation_requires_live_driver_transaction():
    session, driver = bridge_session()
    value = token(uuid.uuid4(), uuid.uuid4())
    prepared = await envelope.prepare_envelope(session, value)
    driver.is_in_transaction = lambda: False
    with pytest.raises(ValueError, match='activation_prepared_transaction_changed'):
        await envelope.validation_envelope(session, value, prepared)
    driver.execute.assert_not_awaited()


async def test_failed_do_never_reads_receipt():
    class Failure(Exception):
        sqlstate = 'P0001'
    session, driver = bridge_session()
    value = token(uuid.uuid4(), uuid.uuid4())
    prepared = await envelope.prepare_envelope(session, value)
    driver.execute.side_effect = Failure('audited_rows_changed')
    with pytest.raises(envelope.promotion.Blocked, match='audited_rows_changed'):
        await envelope.validation_envelope(session, value, prepared)
    session.scalar.assert_not_awaited()


async def test_prepared_payload_rejects_reaudit_same_manifest_with_new_versions():
    session, driver = bridge_session()
    value = token(uuid.uuid4(), uuid.uuid4())
    prepared = await envelope.prepare_envelope(session, value)
    value.row_versions['profiles'] = {'count': 1, 'signature': 'b'*64}
    with pytest.raises(envelope.promotion.Blocked, match='preparation_changed_since_audit'):
        await envelope.validation_envelope(session, value, prepared)
    driver.execute.assert_not_awaited()


@pytest.mark.parametrize('changed', ['transaction', 'driver'])
async def test_prepared_envelope_cannot_move_to_another_driver_or_transaction(changed):
    session, driver = bridge_session()
    value = token(uuid.uuid4(), uuid.uuid4())
    prepared = await envelope.prepare_envelope(session, value)
    connection = await session.connection()
    if changed == 'transaction':
        connection.sync_connection.get_nested_transaction = lambda: object()
    else:
        connection.get_raw_connection = AsyncMock(return_value=SimpleNamespace(
            driver_connection=SimpleNamespace(is_in_transaction=lambda: True, execute=AsyncMock())))
    with pytest.raises(ValueError, match='activation_prepared_transaction_changed'):
        await envelope.validation_envelope(session, value, prepared)
    driver.execute.assert_not_awaited()


async def test_revision_capture_requires_every_audited_boundary_and_nonnegative_clock():
    from tests.test_guard_context_release_prepare import Result
    value = token(uuid.uuid4(), uuid.uuid4())
    uid = uuid.uuid4()
    plan = envelope.promotion.UserPlan({'user_id': uid}, [], [], {}, 5, '')
    rows = [{'user_id':uid,'through_message_id':5,'revision':9}]
    session = SimpleNamespace(execute=AsyncMock(return_value=Result(rows)))
    assert await envelope.promotion.audited_source_revisions(session, value.preparation_id, [plan]) == {str(uid):9}
    query = str(session.execute.await_args.args[0])
    assert 'count(*) AS total' in query and 'r.revision+COALESCE(e.total,0)' in query
    assert 'MAX(id)' not in query and 'max(id)' not in query
    for malformed in ([], [{**rows[0],'through_message_id':4}], [{**rows[0],'revision':-1}]):
        session.execute.return_value = Result(malformed)
        with pytest.raises(envelope.promotion.Blocked, match='preparation_revision_unregistered'):
            await envelope.promotion.audited_source_revisions(session, value.preparation_id, [plan])


async def test_revision_enrollment_only_advances_clock_when_boundary_changes():
    session = SimpleNamespace(execute=AsyncMock(), scalar=AsyncMock(return_value=0))
    value = token(uuid.uuid4(), uuid.uuid4())
    await envelope.promotion.enroll_source_revisions(session, value.preparation_id, value.manifest)
    assert session.execute.await_count == 3
    assert 'FOR UPDATE NOWAIT' in str(session.scalar.await_args.args[0])
    first = session.execute.await_args_list[1]
    assert 'FOR UPDATE NOWAIT' in str(first.args[0])
    sql, params = session.execute.await_args.args
    assert 'revision=retention_preparation_revisions.revision+1' in str(sql)
    assert 'IS DISTINCT FROM EXCLUDED.through_message_id' in str(sql)
    assert set(json.loads(params['targets'])[0]) == {'user_id','through'}
    assert 'UPDATE messages' not in str(sql)
