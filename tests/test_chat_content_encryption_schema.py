"""Reviewed additive metadata; PostgreSQL negative-shape tests run separately in dev."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import tomllib

import pytest
from sqlalchemy import text

from db import schema_contract as sc
from app.models.message import Message
from app.models.greeting import Greeting
from app.models.idempotency_key import IdempotencyKey

ROOT = Path(__file__).resolve().parents[1]
DDL = ROOT / 'db/changes/chat_content_encryption.sql'


def baseline():
    old = deepcopy(sc.load_contract())
    for key in sc._ENCRYPTION_COLUMNS:
        del old['columns'][key]
    for key in sc._ENCRYPTION_CONSTRAINT_KEYS:
        del old['constraints'][key]
    for key in ('public.messages.content', 'public.greetings.content'):
        old['columns'][key]['not_null'] = True
    key = 'public.begin_subject_deletion(uuid,uuid)'
    old['functions'][key]['definition'] = old['functions'][key]['definition'].replace(
        "SET response = NULL, response_ciphertext = NULL, terminal_status",
        "SET response = NULL, terminal_status")
    return old


def test_exact_reviewed_catalog_only_changes_four_columns_two_nullable_checks_and_deletion():
    old = baseline()
    expected = sc.reviewed_encryption_catalog_upgrade(old, constraints=sc._ENCRYPTION_CONSTRAINTS)
    assert sc.load_contract() == expected
    added = expected['columns'].keys() - old['columns'].keys()
    assert added == sc._ENCRYPTION_COLUMNS.keys()
    for section in old:
        if section not in {'columns', 'constraints', 'functions'}:
            assert old[section] == expected[section]
    assert len(expected['constraints']) == len(old['constraints']) + 3
    changed_functions = [key for key in old['functions'] if old['functions'][key] != expected['functions'][key]]
    assert changed_functions == ['public.begin_subject_deletion(uuid,uuid)']
    for key in old['columns']:
        if key not in {'public.messages.content', 'public.greetings.content'}:
            assert old['columns'][key] == expected['columns'][key]
    assert expected['functions'][changed_functions[0]]['owner'] == old['functions'][changed_functions[0]]['owner']


def test_nullable_orm_fields_and_no_default_backfill():
    for model, fields in [(Message, ['content_ciphertext', 'content_chars']), (Greeting, ['content_ciphertext']),
                          (IdempotencyKey, ['response_ciphertext'])]:
        for field in fields:
            col = model.__table__.c[field]
            assert col.nullable and col.default is None and col.server_default is None
    assert Message.__table__.c.content.nullable and Greeting.__table__.c.content.nullable


def test_not_valid_checks_no_existing_row_rewrite_or_new_table_or_index():
    ddl = DDL.read_text()
    assert len(re.findall(r'ADD COLUMN ', ddl)) == 4
    assert len(re.findall(r'ADD CONSTRAINT .*?NOT VALID;', ddl, re.S)) == 3
    assert 'VALIDATE CONSTRAINT' not in ddl and 'CREATE TABLE' not in ddl and 'CREATE INDEX' not in ddl
    assert 'UPDATE public.messages' not in ddl and 'UPDATE public.greetings' not in ddl
    assert "SET response = NULL, response_ciphertext = NULL, terminal_status = 'redacted'" in ddl
    assert '"format_version":1' in ddl and '"started":false' in ddl
    assert 'ON CONFLICT (key) DO NOTHING' in ddl
    for value in sc._ENCRYPTION_CONSTRAINTS.values():
        assert value['validated'] is False and value['definition'].endswith('NOT VALID')


def test_checks_close_null_and_short_header_boundaries():
    ddl = DDL.read_text()
    assert 'content_chars IS NOT NULL' in ddl and 'content_chars IS NULL' in ddl
    assert 'response IS NOT NULL AND response = ' in ddl
    assert 'response IS NULL AND response_ciphertext IS NULL' in ddl
    assert 'redacted_at IS NOT NULL OR terminal_status IN' in ddl
    assert 'CASE WHEN octet_length' in ddl and 'ELSE false END' in ddl
    assert 'BETWEEN 32 AND 1048608' in ddl and 'BETWEEN 32 AND 4194336' in ddl
    assert "decode('0100','hex')" in ddl
    assert 'get_byte(content_ciphertext,2) > 0 OR get_byte(content_ciphertext,3) > 0' in ddl
    assert 'content_chars*4' in ddl


def test_pre_activation_rollback_never_discards_ciphertexts():
    ddl = (ROOT / 'db/changes/chat_content_encryption_rollback.sql').read_text()
    assert "(v_state->>'started')::boolean IS DISTINCT FROM false" in ddl
    assert 'response_schema_version=3' in ddl
    assert ddl.index('content_encryption_rollback_forbidden') < ddl.index('DROP COLUMN')
    assert 'ACCESS EXCLUSIVE MODE NOWAIT' in ddl
    assert 'SET content =' not in ddl


@pytest.mark.parametrize('change', ['validated', 'definition', 'extra'])
def test_reviewed_derivation_rejects_other_definitions(change):
    constraints = deepcopy(sc._ENCRYPTION_CONSTRAINTS)
    key = next(iter(constraints))
    if change == 'extra':
        constraints['public.unrelated.check'] = constraints[key]
    else:
        constraints[key][change] = True if change == 'validated' else 'CHECK (true) NOT VALID'
    with pytest.raises(ValueError):
        sc.reviewed_encryption_catalog_upgrade(baseline(), constraints=constraints)


def test_direct_dependency_and_lock_remain_exact_49():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    lock = tomllib.loads((ROOT / 'uv.lock').read_text())
    assert 'cryptography==49.0.0' in project['project']['dependencies']
    assert [x['version'] for x in lock['package'] if x['name'] == 'cryptography'] == ['49.0.0']
    package = next(x for x in lock['package'] if x['name'] == 'moly-backend')
    assert {'name': 'cryptography'} in package['dependencies']
    assert {'name': 'cryptography', 'specifier': '==49.0.0'} in package['metadata']['requires-dist']


def test_schema_hash_matches_derived_contract():
    artifact = json.loads(sc.CONTRACT.read_text())
    assert artifact['schema_sha256'] == hashlib.sha256(sc.SCHEMA.read_bytes()).hexdigest()
    # Check strings are SQL literals, not parameters. DDL execution uses raw driver SQL.
    assert not text("SELECT '{}'::jsonb")._bindparams
