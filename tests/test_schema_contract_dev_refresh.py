"""Reviewed dev catalog refresh must not turn unrelated live drift into the baseline."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from db import schema_contract as sc
from db.catalog import QUERIES
from db.envfile import DEV_PROJECT_REFS


REF = sorted(DEV_PROJECT_REFS)[0]
DSN = f'postgresql://postgres.{REF}@aws-0-test.pooler.supabase.com:6543/postgres'


def _baseline_source():
    return (
        '    checkpoint_source_hash text,\n'
        "    publish_state text DEFAULT 'published'::text NOT NULL,\n"
        '    last_reconsolidated_at timestamp with time zone,\n'
        '    category text,\n    CONSTRAINT mem0_ingest_candidates_status_check\n'
        '    CONSTRAINT messages_kind_check ' + sc._OLD_KIND_DEFINITION + ',\n'
    )


class _ReadOnlyTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _CatalogConnection:
    def __init__(self, catalog):
        self.catalog = catalog
        self.transactions = []
        self.closed = False
        self.on_read = None

    def transaction(self, **kwargs):
        self.transactions.append(kwargs)
        assert kwargs == {'isolation': 'repeatable_read', 'readonly': True}
        return _ReadOnlyTransaction()

    async def execute(self, sql):
        assert sql == "SET LOCAL search_path = ''", 'refresh must never execute DDL'

    async def fetch(self, query, schemas):
        assert schemas == ['public', 'vecs']
        section = next(name for name, sql in QUERIES.items() if sql == query)
        if self.on_read is not None:
            callback, self.on_read = self.on_read, None
            callback()
        return [{'key': key, **value} for key, value in self.catalog[section].items()]

    async def close(self):
        self.closed = True


@pytest.fixture
def refresh(monkeypatch, tmp_path):
    baseline = {section: {} for section in QUERIES}
    baseline['tables'] = {
        f'public.{name}': {'owner': 'postgres', 'relrowsecurity': True}
        for name in ['messages', 'chat_contexts', 'conversation_checkpoints',
                     'mem0_memory_registry', 'mem0_ingest_candidates', 'products']
    }
    baseline['columns']['public.products.price_hay'] = sc._column('bigint')
    baseline['constraints']['public.messages.messages_kind_check'] = sc._constraint(
        sc._OLD_KIND_DEFINITION,
    )
    baseline['functions']['public.preserve_me()'] = {
        'owner': 'postgres', 'definition': 'original executable definition',
    }
    source = tmp_path / 'schema.sql'
    source.write_text(sc.reviewed_schema_upgrade(_baseline_source()))
    previous = tmp_path / 'baseline.sql'
    previous.write_text(_baseline_source())
    artifact = tmp_path / 'schema_contract.json'
    artifact.write_text(json.dumps({
        'schema_sha256': hashlib.sha256(previous.read_bytes()).hexdigest(), 'catalog': baseline,
    }))
    reviewed = tmp_path / 'crisis_guardrails.sql'
    reviewed.write_text('-- Reviewed DDL is never executed by this command.\n')
    monkeypatch.setattr(sc, 'SCHEMA', source)
    monkeypatch.setattr(sc, 'CONTRACT', artifact)
    monkeypatch.setattr(sc, 'REVIEWED_DEV_SQL', reviewed)
    connection = _CatalogConnection(sc.reviewed_catalog_upgrade(baseline))
    calls = []

    async def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return connection

    monkeypatch.setattr(sc.asyncpg, 'connect', connect)
    options = {
        'env': 'dev', 'expected_project_ref': REF, 'reviewed_sql': reviewed,
        'expected_sha256': hashlib.sha256(reviewed.read_bytes()).hexdigest(),
        'baseline_schema': previous,
    }
    return SimpleNamespace(
        options=options, baseline=baseline, connection=connection, calls=calls,
        artifact=artifact, source=source, previous=previous, reviewed=reviewed,
    )


async def test_refresh_is_read_only_and_does_not_absorb_unrelated_additive_tables(refresh):
    refresh.connection.catalog['tables']['public.unrelated_rollout'] = {'owner': 'postgres'}
    refresh.connection.catalog['columns']['public.products.unrelated_nullable'] = sc._column('text')
    result = await sc.refresh_from_dev(DSN, **refresh.options)

    assert result == []
    generated = json.loads(refresh.artifact.read_text())
    assert 'public.unrelated_rollout' not in generated['catalog']['tables']
    assert 'public.products.unrelated_nullable' not in generated['catalog']['columns']
    assert generated['catalog']['functions'] == refresh.baseline['functions']
    assert len(generated['catalog']['columns']) == len(refresh.baseline['columns']) + 7
    assert generated['schema_sha256'] == hashlib.sha256(refresh.source.read_bytes()).hexdigest()
    assert refresh.connection.transactions == [
        {'isolation': 'repeatable_read', 'readonly': True},
    ]
    assert refresh.connection.closed


@pytest.mark.parametrize(('section', 'key', 'field', 'value'), [
    ('columns', 'public.products.price_hay', 'type', 'text'),
    ('columns', 'public.mem0_memory_registry.context_priority', 'default', "'required'::text"),
    ('constraints', 'public.messages.messages_kind_check', 'definition', 'CHECK (true)'),
    ('constraints', 'public.mem0_ingest_candidates.mem0_ingest_candidates_context_priority_check',
     'validated', False),
    ('functions', 'public.preserve_me()', 'owner', 'anon'),
])
async def test_any_unreviewed_drift_or_wrong_reviewed_definition_blocks_refresh(
    refresh, section, key, field, value,
):
    before = refresh.artifact.read_bytes()
    refresh.connection.catalog[section][key][field] = value

    assert await sc.refresh_from_dev(DSN, **refresh.options)
    assert refresh.artifact.read_bytes() == before
    assert refresh.connection.closed


async def test_extra_grants_on_existing_table_are_not_blessed(refresh):
    before = refresh.artifact.read_bytes()
    refresh.connection.catalog['relation_grants']['public.products:anon:UPDATE'] = {
        'grantable': False,
    }
    assert await sc.refresh_from_dev(DSN, **refresh.options)
    assert refresh.artifact.read_bytes() == before


@pytest.mark.parametrize('overrides', [
    {'env': 'prod'}, {'env': '.env'}, {'expected_project_ref': 'unknownproject'},
    {'expected_sha256': '0' * 64}, {'expected_sha256': ''},
])
async def test_refresh_guards_fail_before_connection(refresh, overrides):
    options = refresh.options | overrides
    with pytest.raises(ValueError):
        await sc.refresh_from_dev(DSN, **options)
    assert refresh.calls == []


@pytest.mark.parametrize('dsn', [
    f'postgresql://postgres.{REF}@untrusted.example/postgres',
    f'postgresql://postgres.{REF}@aws-0-test.pooler.supabase.com/postgres?host=prod.example',
    f'postgresql://postgres.{REF}@aws-0-test.pooler.supabase.com/postgres#override',
    f'postgresql://postgres.{REF}@localhost/postgres',
    f'postgresql://postgres.{REF}@aws-0-test.pooler.supabase.com/other_database',
    'postgresql://postgres.production@aws-0-test.pooler.supabase.com/postgres',
])
async def test_dev_ref_cannot_authorize_another_host_or_override(refresh, dsn):
    with pytest.raises(ValueError):
        await sc.refresh_from_dev(dsn, **refresh.options)
    assert refresh.calls == []


async def test_unreviewed_schema_edits_fail_before_connection(refresh):
    refresh.source.write_text(refresh.source.read_text() + '\n-- unrelated source edit\n')
    with pytest.raises(ValueError, match='outside the reviewed'):
        await sc.refresh_from_dev(DSN, **refresh.options)
    assert refresh.calls == []


async def test_baseline_must_match_existing_generated_contract(refresh):
    refresh.previous.write_text('not the schema used by the existing artifact')
    with pytest.raises(ValueError, match='baseline schema'):
        await sc.refresh_from_dev(DSN, **refresh.options)
    assert refresh.calls == []


async def test_actual_dev_must_already_have_the_reviewed_change(refresh):
    refresh.connection.catalog = deepcopy(refresh.baseline)
    before = refresh.artifact.read_bytes()
    assert await sc.refresh_from_dev(DSN, **refresh.options)
    assert refresh.artifact.read_bytes() == before


async def test_reviewed_inputs_cannot_change_while_catalog_is_read(refresh):
    before = refresh.artifact.read_bytes()
    refresh.connection.on_read = lambda: refresh.reviewed.write_text('-- changed while reading\n')
    with pytest.raises(ValueError, match='changed during catalog'):
        await sc.refresh_from_dev(DSN, **refresh.options)
    assert refresh.artifact.read_bytes() == before
    assert list(refresh.artifact.parent.glob('*.dev-refresh-*')) == []


async def test_a_different_sql_path_is_not_approved_by_a_matching_hash(refresh, tmp_path):
    alternate = tmp_path / 'different.sql'
    alternate.write_bytes(refresh.reviewed.read_bytes())
    with pytest.raises(ValueError, match='only this release'):
        await sc.refresh_from_dev(DSN, **(refresh.options | {'reviewed_sql': alternate}))
    assert refresh.calls == []


async def test_strict_refresh_does_not_ignore_additive_objects(refresh):
    before = refresh.artifact.read_bytes()
    refresh.connection.catalog['tables']['public.unrelated_rollout'] = {'owner': 'postgres'}
    assert await sc.refresh_from_dev(DSN, **refresh.options, strict=True)
    assert refresh.artifact.read_bytes() == before


def test_cli_refuses_prod_before_loading_any_credentials(monkeypatch):
    monkeypatch.setattr('sys.argv', ['schema_contract', '--refresh-from-dev', '--env', 'prod'])
    from db import envfile
    monkeypatch.setattr(envfile, 'load_conn', lambda *_: pytest.fail('must refuse before loading DSN'))
    with pytest.raises(SystemExit) as error:
        sc.main()
    assert error.value.code == 2
