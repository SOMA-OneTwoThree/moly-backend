"""Generated catalog contract for schema.sql; production checks are read-only."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlparse

import asyncpg

from db.catalog import compare_catalog, read_catalog

SCHEMA = Path(__file__).with_name('schema.sql')
CONTRACT = Path(__file__).with_name('schema_contract.json')
SUBSCRIPTION_SQL = Path(__file__).with_name('changes') / 'subscription_refund_access.sql'
SUBSCRIPTION_COLUMNS = ('public.subscriptions.access_evidence', 'public.revenuecat_events.processing_state')


def subscription_schema_upgrade(baseline: str) -> str:
    upgraded = baseline
    for old, new in (
        ('CREATE TABLE public.revenuecat_events (\n    event_id text NOT NULL,\n    payload jsonb NOT NULL,\n',
         'CREATE TABLE public.revenuecat_events (\n    event_id text NOT NULL,\n    payload jsonb NOT NULL,\n    processing_state jsonb,\n'),
        ('    last_event_at timestamp with time zone,\n    CONSTRAINT subscriptions_plan_check',
         '    last_event_at timestamp with time zone,\n    access_evidence jsonb,\n    CONSTRAINT subscriptions_plan_check'),
    ):
        if upgraded.count(old) != 1:
            raise ValueError('unexpected production subscription schema baseline')
        upgraded = upgraded.replace(old, new, 1)
    return upgraded


async def refresh_subscription_from_dev(*, env: str, expected_project_ref: str, reviewed_sql: Path,
                                        expected_sha256: str, baseline_schema: Path) -> list[str]:
    """Derive only two reviewed columns from dev; preserve the MAIN catalog in full.

    Dev and main intentionally differ in other features. Never import the dev
    catalog wholesale or certify those differences as production changes.
    """
    from db.envfile import DEV_PROJECT_REFS, assert_dev_target, load_conn, project_ref

    if env != 'dev' or expected_project_ref not in DEV_PROJECT_REFS:
        raise ValueError('explicit confirmed development source required')
    dsn = load_conn(env)
    assert_dev_target(env, dsn)
    target = urlparse(dsn)
    if (target.path != '/postgres' or target.port not in {None, 5432, 6543}
            or not ((target.hostname or '').endswith('.pooler.supabase.com')
                    or target.hostname == f'db.{expected_project_ref}.supabase.co')):
        raise ValueError('confirmed development connection endpoint required')
    if project_ref(dsn) != expected_project_ref or reviewed_sql.resolve() != SUBSCRIPTION_SQL.resolve():
        raise ValueError('reviewed subscription source mismatch')
    sql = reviewed_sql.read_bytes()
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256) or hashlib.sha256(sql).hexdigest() != expected_sha256:
        raise ValueError('reviewed subscription SQL checksum mismatch')
    original_contract, source = CONTRACT.read_bytes(), SCHEMA.read_bytes()
    baseline = baseline_schema.read_bytes()
    contract = json.loads(original_contract)
    if hashlib.sha256(baseline).hexdigest() != contract['schema_sha256']:
        raise ValueError('production baseline checksum mismatch')
    if subscription_schema_upgrade(baseline.decode()).encode() != source:
        raise ValueError('schema contains changes outside subscription release')
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=15)
    try:
        actual = await read_catalog(conn)  # Owns a repeatable-read, read-only transaction.
    finally:
        await conn.close()
    expected_column = {'type': 'jsonb', 'not_null': False, 'default': None,
                       'identity': '', 'generated': '', 'collation': None}
    result = deepcopy(contract['catalog'])
    for key in SUBSCRIPTION_COLUMNS:
        if key in result['columns'] or actual['columns'].get(key) != expected_column:
            return ['reviewed nullable JSONB column mismatch: ' + key]
        result['columns'][key] = deepcopy(actual['columns'][key])
    generated = json.dumps({'schema_sha256': hashlib.sha256(source).hexdigest(), 'catalog': result},
                           ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    if (CONTRACT.read_bytes() != original_contract or SCHEMA.read_bytes() != source
            or reviewed_sql.read_bytes() != sql or baseline_schema.read_bytes() != baseline):
        raise ValueError('reviewed release inputs changed during metadata inspection')
    temporary = CONTRACT.with_name('.subscription-contract-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(generated)
        os.replace(temporary, CONTRACT)
    finally:
        temporary.unlink(missing_ok=True)
    return []



# Guard/context manifests augment the main catalog; unrelated release helpers remain intact.
REVIEWED_DEV_SQL = Path(__file__).with_name('changes') / 'crisis_guardrails.sql'


REVIEWED_GUARD_CONTEXT_SQL = Path(__file__).with_name('changes') / 'guard_context_v2.sql'


def _column(sql_type: str, *, not_null: bool = False, default: str | None = None) -> dict:
    return {
        'type': sql_type, 'not_null': not_null, 'default': default,
        'identity': '', 'generated': '', 'collation': '"default"' if sql_type == 'text' else None,
    }


DEV_REFRESH_COLUMNS = {
    'public.chat_contexts.contract_processed_through_message_id':
        _column('bigint', not_null=True, default='0'),
    'public.chat_contexts.contract_generation': _column('bigint', not_null=True, default='0'),
    'public.conversation_checkpoints.state_json': _column('jsonb'),
    'public.conversation_checkpoints.validation_json': _column('jsonb'),
    'public.mem0_memory_registry.canonical_text': _column('text'),
    'public.mem0_memory_registry.context_priority':
        _column('text', not_null=True, default="'related'::text"),
    'public.mem0_ingest_candidates.context_priority':
        _column('text', not_null=True, default="'related'::text"),
}


_OLD_KINDS = (
    "'normal'::text, 'greeting'::text, 'fortune_context_root'::text, "
    "'fortune_derived'::text, 'topic_opening'::text"
)


_NEW_KINDS = _OLD_KINDS + ", 'safety_check'::text, 'safety'::text"


_OLD_KIND_DEFINITION = f'CHECK ((kind = ANY (ARRAY[{_OLD_KINDS}])))'


_NEW_KIND_DEFINITION = f'CHECK ((kind = ANY (ARRAY[{_NEW_KINDS}])))'


_PRIORITY_DEFINITION = "CHECK ((context_priority = ANY (ARRAY['related'::text, 'required'::text])))"


def _constraint(definition: str) -> dict:
    return {'definition': definition, 'validated': True, 'deferrable': False,
            'deferred': False, 'enforcement_triggers': []}


DEV_REFRESH_CONSTRAINTS = {
    'public.messages.messages_kind_check': _constraint(_NEW_KIND_DEFINITION),
    'public.mem0_memory_registry.mem0_memory_registry_context_priority_check':
        _constraint(_PRIORITY_DEFINITION),
    'public.mem0_ingest_candidates.mem0_ingest_candidates_context_priority_check':
        _constraint(_PRIORITY_DEFINITION),
}


def reviewed_schema_upgrade(baseline: str) -> str:
    """Apply only this release's exact source edits to the hash-verified baseline."""
    edits = [
        ('    checkpoint_source_hash text,\n',
         '    checkpoint_source_hash text,\n'
         '    contract_processed_through_message_id bigint DEFAULT 0 NOT NULL,\n'
         '    contract_generation bigint DEFAULT 0 NOT NULL,\n'),
        ("    publish_state text DEFAULT 'published'::text NOT NULL,\n",
         "    publish_state text DEFAULT 'published'::text NOT NULL,\n"
         '    state_json jsonb,\n    validation_json jsonb,\n'),
        ('    last_reconsolidated_at timestamp with time zone,\n',
         '    last_reconsolidated_at timestamp with time zone,\n'
         '    canonical_text text,\n'
         "    context_priority text DEFAULT 'related'::text NOT NULL,\n"
         '    CONSTRAINT mem0_memory_registry_context_priority_check '
         + _PRIORITY_DEFINITION + ',\n'),
        ('    category text,\n    CONSTRAINT mem0_ingest_candidates_status_check',
         "    category text,\n    context_priority text DEFAULT 'related'::text NOT NULL,\n"
         '    CONSTRAINT mem0_ingest_candidates_context_priority_check '
         + _PRIORITY_DEFINITION + ',\n    CONSTRAINT mem0_ingest_candidates_status_check'),
        ('    CONSTRAINT messages_kind_check ' + _OLD_KIND_DEFINITION + ',\n',
         '    CONSTRAINT messages_kind_check ' + _NEW_KIND_DEFINITION + ',\n'),
    ]
    upgraded = baseline
    for old, new in edits:
        if new in upgraded:
            # Idempotent re-check of an artifact already generated by this mode.
            if upgraded.count(new) != 1:
                raise ValueError('ambiguous reviewed schema edit')
            continue
        if upgraded.count(old) != 1:
            raise ValueError('baseline does not match this reviewed release')
        upgraded = upgraded.replace(old, new, 1)
    return upgraded


def reviewed_catalog_upgrade(baseline: dict) -> dict:
    """Preserve the baseline, changing only explicitly reviewed object definitions."""
    expected = deepcopy(baseline)
    previous = expected['constraints'].get('public.messages.messages_kind_check')
    if previous not in (_constraint(_OLD_KIND_DEFINITION), _constraint(_NEW_KIND_DEFINITION)):
        raise ValueError('unexpected baseline message constraint')
    for section, changes in (
        ('columns', DEV_REFRESH_COLUMNS), ('constraints', DEV_REFRESH_CONSTRAINTS),
    ):
        for key, definition in changes.items():
            if key.rsplit('.', 1)[0] not in expected['tables']:
                raise ValueError('reviewed object table is missing')
            if (key in expected[section] and key != 'public.messages.messages_kind_check'
                    and expected[section][key] != definition):
                raise ValueError('baseline has a conflicting reviewed object')
            expected[section][key] = deepcopy(definition)
    return expected


_PRESSURE_CHECK = 'CHECK (((retention_pressure_through_message_id IS NULL) OR (retention_pressure_through_message_id > 0)))'


_PRESSURE_FUNCTIONS = {
    'reset_retention_pressure_generation': (False, """BEGIN
  IF NEW.memory_generation IS DISTINCT FROM OLD.memory_generation THEN
    NEW.retention_pressure_through_message_id := NULL;
  END IF;
  RETURN NEW;
END;
"""),
    'reset_retention_pressure_privacy': (True, """BEGIN
  IF NEW.state <> 'active' OR (TG_OP = 'UPDATE' AND NEW.epoch IS DISTINCT FROM OLD.epoch) THEN
    UPDATE public.chat_contexts SET retention_pressure_through_message_id = NULL
    WHERE user_id = NEW.user_id AND retention_pressure_through_message_id IS NOT NULL;
  END IF;
  RETURN NEW;
END;
"""),
}


_PRESSURE_TRIGGERS = {
    'public.chat_contexts.chat_contexts_retention_pressure_generation':
        'CREATE TRIGGER chat_contexts_retention_pressure_generation BEFORE UPDATE OF memory_generation ON public.chat_contexts FOR EACH ROW EXECUTE FUNCTION public.reset_retention_pressure_generation()',
    'public.privacy_subject_barriers.privacy_subject_barriers_retention_pressure':
        'CREATE TRIGGER privacy_subject_barriers_retention_pressure AFTER INSERT OR UPDATE OF state, epoch ON public.privacy_subject_barriers FOR EACH ROW EXECUTE FUNCTION public.reset_retention_pressure_privacy()',
}


def reviewed_guard_context_schema_upgrade(baseline: str) -> str:
    functions = ''.join(
        f'-- FUNCTION: public.{name}()\nCREATE FUNCTION public.{name}() RETURNS trigger\n'
        f"    LANGUAGE plpgsql{' SECURITY DEFINER' if secure else ''}\n    SET search_path TO ''\n"
        f'    AS $function$\n{body}$function$;\n\n'
        for name, (secure, body) in _PRESSURE_FUNCTIONS.items())
    triggers = ''.join(
        f"-- TRIGGER: {key.rsplit('.', 1)[0]} {key.rsplit('.', 1)[1]}\n{definition};\n\n"
        for key, definition in _PRESSURE_TRIGGERS.items())
    grants = ''.join(
        f'-- ACL: public.FUNCTION {name}()\n'
        f'REVOKE ALL ON FUNCTION public.{name}() FROM PUBLIC, anon, authenticated, service_role;\n\n'
        for name in _PRESSURE_FUNCTIONS)
    edits = (
        ('    contract_generation bigint DEFAULT 0 NOT NULL,\n',
         '    contract_generation bigint DEFAULT 0 NOT NULL,\n'
         '    retention_pressure_through_message_id bigint,\n'
         '    CONSTRAINT chat_contexts_retention_pressure_positive_check ' + _PRESSURE_CHECK + ',\n'),
        ('-- FUNCTION: public.guard_normalized_memory_snapshot()\n', functions + '-- FUNCTION: public.guard_normalized_memory_snapshot()\n'),
        ('-- TRIGGER: public.orders orders_set_updated_at\n', triggers + '-- TRIGGER: public.orders orders_set_updated_at\n'),
        ('-- ACL: public.FUNCTION guard_normalized_memory_snapshot()\n', grants + '-- ACL: public.FUNCTION guard_normalized_memory_snapshot()\n'),
    )
    result = baseline
    for old, new in edits:
        if new in result:
            if result.count(new) != 1:
                raise ValueError('ambiguous guard context schema edit')
            continue
        if result.count(old) != 1:
            raise ValueError('baseline does not match guard context release')
        result = result.replace(old, new, 1)
    return result


def reviewed_guard_context_catalog_upgrade(baseline: dict) -> dict:
    expected = deepcopy(baseline)
    changes = {
        'columns': {'public.chat_contexts.retention_pressure_through_message_id': _column('bigint')},
        'constraints': {'public.chat_contexts.chat_contexts_retention_pressure_positive_check': _constraint(_PRESSURE_CHECK)},
        'triggers': {key: {'definition': definition, 'enabled': 'O'}
                     for key, definition in _PRESSURE_TRIGGERS.items()},
        'functions': {f'public.{name}()': {
            'definition': f'CREATE OR REPLACE FUNCTION public.{name}()\n RETURNS trigger\n'
                f" LANGUAGE plpgsql\n{' SECURITY DEFINER' + chr(10) if secure else ''} SET search_path TO ''\n"
                f'AS $function$\n{body}$function$\n',
            'owner': baseline['tables']['public.chat_contexts']['owner'],
        } for name, (secure, body) in _PRESSURE_FUNCTIONS.items()},
    }
    for section, entries in changes.items():
        for key, definition in entries.items():
            if key in expected[section] and expected[section][key] != definition:
                raise ValueError('conflicting guard context release object')
            expected[section][key] = definition
    # New trigger helpers have no direct execution grant; preserve all existing ACLs.
    for key in expected['function_grants']:
        if any(key.startswith(f'public.{name}():') for name in _PRESSURE_FUNCTIONS):
            raise ValueError('unexpected guard context function grant')
    return expected


def require_reviewed_dev(dsn: str, *, env: str, expected_project_ref: str) -> None:
    """Refuse prod, unknown hosts/refs, and URL connection overrides before connecting."""
    from db.envfile import DEV_PROJECT_REFS, project_ref

    target = urlparse(dsn)
    host = target.hostname or ''
    if (env != 'dev' or expected_project_ref not in DEV_PROJECT_REFS
            or project_ref(dsn) != expected_project_ref
            or target.scheme not in {'postgres', 'postgresql'}
            or target.query or target.fragment or target.path != '/postgres'
            or target.port not in {None, 5432, 6543}
            or not (host.endswith('.pooler.supabase.com')
                    or host == f'db.{expected_project_ref}.supabase.co')):
        raise ValueError('refresh requires the explicitly confirmed development Supabase target')


async def refresh_from_dev(
    dsn: str, *, env: str, expected_project_ref: str, reviewed_sql: Path,
    expected_sha256: str, baseline_schema: Path, strict: bool = False,
) -> list[str]:
    """Read dev only; regenerate this release without adopting unrelated live drift."""
    require_reviewed_dev(dsn, env=env, expected_project_ref=expected_project_ref)
    guard_context_release = reviewed_sql.resolve() == REVIEWED_GUARD_CONTEXT_SQL.resolve()
    if not guard_context_release and reviewed_sql.resolve() != REVIEWED_DEV_SQL.resolve():
        raise ValueError('only this release reviewed SQL is supported')
    sql_bytes = reviewed_sql.read_bytes()
    if (not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
            or hashlib.sha256(sql_bytes).hexdigest() != expected_sha256):
        raise ValueError('reviewed SQL checksum mismatch')
    original_contract = CONTRACT.read_bytes()
    original_schema = SCHEMA.read_bytes()
    original_baseline = baseline_schema.read_bytes()
    contract = json.loads(original_contract)
    if hashlib.sha256(original_baseline).hexdigest() != contract['schema_sha256']:
        raise ValueError('baseline schema does not match the existing generated contract')
    source_upgrade = reviewed_guard_context_schema_upgrade if guard_context_release else reviewed_schema_upgrade
    catalog_upgrade = reviewed_guard_context_catalog_upgrade if guard_context_release else reviewed_catalog_upgrade
    upgraded_source = source_upgrade(original_baseline.decode())
    if upgraded_source.encode() != original_schema:
        raise ValueError('schema.sql contains changes outside the reviewed release')
    expected = catalog_upgrade(contract['catalog'])
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=15)
    try:
        # read_catalog owns a repeatable-read, readonly transaction and only reads
        # PostgreSQL metadata. Never execute reviewed SQL or schema.sql here.
        actual = await read_catalog(conn)
    finally:
        await conn.close()
    problems = compare_catalog(expected, actual, strict=strict)
    if problems:
        return problems
    generated = json.dumps(
        {'schema_sha256': hashlib.sha256(original_schema).hexdigest(), 'catalog': expected},
        ensure_ascii=False, indent=2, sort_keys=True,
    ) + '\n'
    tmp = CONTRACT.with_name(f'.{CONTRACT.name}.dev-refresh-{uuid.uuid4().hex}')
    try:
        with tmp.open('x', encoding='utf-8') as stream:
            stream.write(generated)
        if (CONTRACT.read_bytes() != original_contract or SCHEMA.read_bytes() != original_schema
                or baseline_schema.read_bytes() != original_baseline
                or reviewed_sql.read_bytes() != sql_bytes):
            raise ValueError('reviewed inputs changed during catalog inspection')
        os.replace(tmp, CONTRACT)
    finally:
        tmp.unlink(missing_ok=True)
    return []


GUARD_BASELINE_SCHEMA_SHA256 = 'e5929c8dc95a01842822ff94be432d8df791244ffdbf91820310d3f94a403e7a'
GUARD_BASELINE_CONTRACT_SHA256 = '769d36eb70564f841364b23dbe11ccd63a2b8b075beda1dd26ba856aa206d014'
_GUARD_SQL_DIGESTS = {
    'crisis_guardrails.sql': 'c587c3524e186a8a031c69d5e6769851819ed2a13c4990958f5ebe5466c3da13',
    'guard_context_v2.sql': 'e4c58f65889444430868e676e6d37702733bd30ff0c407c95686937e58145bb6',
}


def derive_guard_context_candidate(*, baseline_schema: Path, baseline_contract: Path,
                                   crisis_sha256: str, pressure_sha256: str) -> list[str]:
    """Derive this release candidate from reviewed main64, without connecting to a DB.

    This is a manifest derivation, not a database attestation. Preserve all main
    catalog objects and require live catalog preflight before applying the SQL.
    The existing live verify/generate and subscription workflows remain separate.
    """
    baseline = baseline_schema.read_bytes()
    original_baseline_contract = baseline_contract.read_bytes()
    if (hashlib.sha256(baseline).hexdigest() != GUARD_BASELINE_SCHEMA_SHA256
            or hashlib.sha256(original_baseline_contract).hexdigest() != GUARD_BASELINE_CONTRACT_SHA256):
        raise ValueError('guard candidate requires the reviewed main64 baseline')
    artifact = json.loads(original_baseline_contract)
    if artifact['schema_sha256'] != hashlib.sha256(baseline).hexdigest():
        raise ValueError('reviewed main baseline source and contract disagree')
    reviewed = ((REVIEWED_DEV_SQL, crisis_sha256), (REVIEWED_GUARD_CONTEXT_SQL, pressure_sha256))
    sql_inputs = []
    for path, expected in reviewed:
        data = path.read_bytes()
        if (expected != _GUARD_SQL_DIGESTS[path.name]
                or hashlib.sha256(data).hexdigest() != expected):
            raise ValueError('guard candidate reviewed SQL checksum mismatch')
        sql_inputs.append((path, data))
    source = SCHEMA.read_bytes()
    upgraded = reviewed_guard_context_schema_upgrade(reviewed_schema_upgrade(baseline.decode()))
    if upgraded.encode() != source:
        raise ValueError('schema contains changes outside the reviewed main guard release')
    catalog = reviewed_guard_context_catalog_upgrade(reviewed_catalog_upgrade(artifact['catalog']))
    candidate = {'schema_sha256': hashlib.sha256(source).hexdigest(), 'catalog': catalog}
    original_current = CONTRACT.read_bytes()
    if (original_current != original_baseline_contract
            and json.loads(original_current) != candidate):
        raise ValueError('current contract is neither reviewed main nor this derived candidate')
    generated = json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    temporary = CONTRACT.with_name('.guard-candidate-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(generated)
        if (baseline_schema.read_bytes() != baseline or SCHEMA.read_bytes() != source
                or baseline_contract.read_bytes() != original_baseline_contract
                or CONTRACT.read_bytes() != original_current
                or any(path.read_bytes() != data for path, data in sql_inputs)):
            raise ValueError('guard candidate reviewed inputs changed during derivation')
        os.replace(temporary, CONTRACT)
    finally:
        temporary.unlink(missing_ok=True)
    return []


def schema_digest() -> str:
    return hashlib.sha256(SCHEMA.read_bytes()).hexdigest()


def load_contract() -> dict:
    contract = json.loads(CONTRACT.read_text())
    if contract['schema_sha256'] != schema_digest():
        raise ValueError('schema.sql changed: regenerate the catalog contract in a fresh local DB')
    return contract['catalog']


async def verify(dsn: str, *, strict: bool = False) -> list[str]:
    expected = load_contract()
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=15)
    try:
        actual = await read_catalog(conn)
    finally:
        await conn.close()
    return compare_catalog(expected, actual, strict=strict)


def require_scratch(dsn: str) -> None:
    target = urlparse(dsn)
    # Query parameters can override the URL host/database in asyncpg. Do not accept
    # alternate connection targets hidden behind an apparently local URL.
    if (target.scheme not in {'postgresql', 'postgres'}
            or target.hostname not in {'localhost', '127.0.0.1', '::1'}
            or not re.fullmatch(r'/moly_schema_[A-Za-z0-9_]+', target.path)
            or target.query or target.fragment):
        raise ValueError('generation requires a local disposable database named moly_schema_*')


async def generate(dsn: str, *, check: bool = False) -> list[str]:
    """Create the schema in a disposable DB, then derive/check the committed contract."""
    require_scratch(dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=60)
    try:
        await conn.execute(SCHEMA.read_text())
        catalog = await read_catalog(conn)
    finally:
        await conn.close()
    if check:
        return compare_catalog(load_contract(), catalog, strict=True)
    CONTRACT.write_text(json.dumps(
        {'schema_sha256': schema_digest(), 'catalog': catalog},
        ensure_ascii=False, indent=2, sort_keys=True,
    ) + '\n')
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strict', action='store_true', help='also reject additive objects')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--generate', action='store_true', help='write contract from fresh local DB')
    modes.add_argument('--check-generated', action='store_true', help='rebuild schema and check contract')
    modes.add_argument('--refresh-subscription-from-dev', action='store_true', help='preserve main baseline and derive two reviewed dev columns')
    modes.add_argument('--refresh-from-dev', action='store_true', help='read-only dev catalog check for a reviewed guard SQL delta')
    modes.add_argument('--derive-guard-context-candidate', action='store_true', help='derive reviewed main64 guard catalog candidate without any DB connection; live preflight remains required')
    parser.add_argument('--env', default='dev')
    parser.add_argument('--expected-project-ref')
    parser.add_argument('--reviewed-sql', type=Path)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--baseline-schema', type=Path)
    parser.add_argument('--baseline-contract', type=Path)
    parser.add_argument('--crisis-sha256')
    parser.add_argument('--pressure-sha256')
    args = parser.parse_args()
    raw = os.environ.get('SUPABASE_DB_CONNECTION_STRING', '')
    if not raw and not args.refresh_subscription_from_dev and not args.derive_guard_context_candidate:
        parser.error('SUPABASE_DB_CONNECTION_STRING is required')
    dsn = re.sub(r'^postgresql\+asyncpg://', 'postgresql://', raw)
    try:
        if args.derive_guard_context_candidate:
            if not all((args.baseline_schema, args.baseline_contract, args.crisis_sha256, args.pressure_sha256)):
                parser.error('reviewed main baseline and both guard SQL checksums required')
            problems = derive_guard_context_candidate(baseline_schema=args.baseline_schema,
                baseline_contract=args.baseline_contract, crisis_sha256=args.crisis_sha256,
                pressure_sha256=args.pressure_sha256)
        elif args.refresh_from_dev:
            if not all((args.expected_project_ref, args.reviewed_sql, args.expected_sha256, args.baseline_schema)):
                parser.error('reviewed guard release arguments required')
            problems = asyncio.run(refresh_from_dev(dsn, env=args.env,
                expected_project_ref=args.expected_project_ref, reviewed_sql=args.reviewed_sql,
                expected_sha256=args.expected_sha256, baseline_schema=args.baseline_schema, strict=args.strict))
        elif args.refresh_subscription_from_dev:
            if not all((args.expected_project_ref, args.reviewed_sql, args.expected_sha256, args.baseline_schema)):
                parser.error('reviewed subscription release arguments required')
            problems = asyncio.run(refresh_subscription_from_dev(
                env=args.env, expected_project_ref=args.expected_project_ref,
                reviewed_sql=args.reviewed_sql, expected_sha256=args.expected_sha256,
                baseline_schema=args.baseline_schema))
        elif args.generate or args.check_generated:
            problems = asyncio.run(generate(dsn, check=args.check_generated))
        else:
            problems = asyncio.run(verify(dsn, strict=args.strict))
    except Exception as exc:
        # Connection errors may contain credentials; only catalog differences are printable.
        print(f'Schema check failed ({type(exc).__name__}); check target and contract generation')
        return 1
    for problem in problems:
        print(problem)
    if not problems:
        print('Schema contract candidate OK (reviewed derivation only; database not checked)'
              if args.derive_guard_context_candidate else 'Schema contract OK')
    return int(bool(problems))


if __name__ == '__main__':
    raise SystemExit(main())
