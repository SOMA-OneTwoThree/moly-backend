"""Metadata-only reviewed expansion controller. Read-only unless --apply is explicit.

No history SELECT/UPDATE, no model/provider call, no key output. The reviewed SQL
is the sole DDL source. Schema drift or partial expansion stops without repair.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.content_crypto import initialize_content_crypto, seal, open_bytes  # noqa: E402
from db.catalog import QUERIES  # noqa: E402
from db.schema_contract import load_contract, _ENCRYPTION_COLUMNS, _ENCRYPTION_CONSTRAINT_KEYS  # noqa: E402
from scripts.activate_chat_content_crypto import (  # noqa: E402
    CONFIG_KEYS, STATE_KEY, PROJECTS, ReleaseBlocked, digest, require_target, snapshot_rows, validate_snapshot,
)

SQL_PATH = Path(__file__).resolve().parents[1] / 'db/changes/chat_content_encryption.sql'
SQL_SHA256 = 'b6b80ea155570de38c9ef4645ec3fc3874e17e67d0cc23604b90eb87112c1f8b'
TABLES = ('public.messages', 'public.greetings', 'public.idempotency_keys')
FUNCTION = 'public.begin_subject_deletion(uuid,uuid)'
FORMAT = 'chat-content-schema-v1'


def _belongs(section, key, value):
    if section == 'functions':
        return key == FUNCTION
    if section == 'function_grants':
        return key.startswith(FUNCTION + ':')
    if section == 'indexes':
        return value.get('table_name') in TABLES
    if section == 'tables':
        return key in TABLES
    if section in {'columns', 'constraints', 'triggers', 'rules', 'policies', 'column_grants'}:
        return any(key.startswith(t + '.') for t in TABLES)
    if section == 'relation_grants':
        return any(key.startswith(t + ':') for t in TABLES)
    return False


def subset(catalog):
    return {section: {k: v for k, v in values.items() if _belongs(section, k, v)}
            for section, values in catalog.items()}


def expected_catalogs():
    after = load_contract()
    before = deepcopy(after)
    for key in _ENCRYPTION_COLUMNS:
        del before['columns'][key]
    for key in _ENCRYPTION_CONSTRAINT_KEYS:
        del before['constraints'][key]
    for key in ('public.messages.content', 'public.greetings.content'):
        before['columns'][key]['not_null'] = True
    f = before['functions'][FUNCTION]
    paired = "SET response = NULL, response_ciphertext = NULL, terminal_status"
    if f['definition'].count(paired) != 1:
        raise ReleaseBlocked('reviewed_schema_source_invalid')
    f['definition'] = f['definition'].replace(paired, 'SET response = NULL, terminal_status', 1)
    return subset(before), subset(after)


def catalog_statement():
    # The exact shared catalog queries are reused; only the affected object set
    # is selected. One network roundtrip also permits checking inside the short
    # DDL transaction without a many-query metadata window.
    expressions = []
    for section, query in QUERIES.items():
        quoted = ','.join("'" + table + "'" for table in TABLES)
        if section == 'tables':
            condition = f'q.key IN ({quoted})'
        elif section == 'indexes':
            condition = f'q.table_name IN ({quoted})'
        elif section == 'functions':
            condition = f"q.key='{FUNCTION}'"
        elif section == 'function_grants':
            condition = f"starts_with(q.key,'{FUNCTION}:')"
        elif section in {'columns', 'constraints', 'triggers', 'rules', 'policies', 'column_grants'}:
            condition = ' OR '.join(f"starts_with(q.key,'{t}.')" for t in TABLES)
        elif section == 'relation_grants':
            condition = ' OR '.join(f"starts_with(q.key,'{t}:')" for t in TABLES)
        else:
            condition = 'false'
        expressions.extend([f"'{section}'", "COALESCE((SELECT jsonb_object_agg(q.key,to_jsonb(q)-'key') FROM ("
            + query + ') q WHERE ' + condition + "),'{}'::jsonb)"])
    return 'SELECT jsonb_build_object(' + ','.join(expressions) + ')'


CATALOG_SQL = catalog_statement()


def normalize_catalog(value):
    if isinstance(value, str):
        value = json.loads(value)
    return {section: {key: {field: sorted(item) if isinstance(item, list) else item
                           for field, item in row.items()} for key, row in rows.items()}
            for section, rows in value.items()}


async def catalog_snapshot(conn):
    return normalize_catalog(await conn.fetchval(CATALOG_SQL, ['public']))


async def config_snapshot(conn, *, allow_missing_marker=False):
    rows = await conn.fetch('SELECT key,value FROM public.app_config WHERE key=ANY($1::text[]) ORDER BY key', list(CONFIG_KEYS))
    result = snapshot_rows([(r['key'], json.loads(r['value']) if isinstance(r['value'], str) else r['value']) for r in rows])
    if allow_missing_marker and not result[STATE_KEY]['exists']:
        checked = {**result, STATE_KEY: {'exists': True, 'value': {
            'started': False, 'active_key_id': 1, 'format_version': 1, 'required_key_ids': [1]}}}
        validate_snapshot(checked)
    else:
        validate_snapshot(result)
    return result


def reviewed_body(path, expected_sha256):
    data = Path(path).read_bytes()
    if expected_sha256 != SQL_SHA256 or hashlib.sha256(data).hexdigest() != SQL_SHA256:
        raise ReleaseBlocked('reviewed_sql_checksum_mismatch')
    # Same enclosing-transaction policy as db.apply. The two source SET limits
    # are removed only after the immutable checksum check; this controller's
    # stricter server limits are established before the outer DO starts.
    body = re.sub(r'^\s*(?:BEGIN|COMMIT);\s*$', '', data.decode(), flags=re.M | re.I)
    for command in ("SET LOCAL lock_timeout = '1s';", "SET LOCAL statement_timeout = '5s';"):
        if body.count(command) != 1:
            raise ReleaseBlocked('reviewed_sql_limits_invalid')
        body = body.replace(command, '', 1)
    sql_tag, do_tag = '$sql_' + uuid.uuid4().hex + '$', '$body_' + uuid.uuid4().hex + '$'
    return f'DO {do_tag} BEGIN EXECUTE {sql_tag}{body}{sql_tag}; END {do_tag}'


def classify(catalog, before, after):
    if catalog == after:
        return 'expanded'
    if catalog == before:
        return 'before'
    raise ReleaseBlocked('reviewed_schema_drift')


async def inspect_connection(conn):
    before, after = expected_catalogs()
    async with conn.transaction(isolation='repeatable_read', readonly=True):
        await conn.execute("SET LOCAL search_path=''")
        catalog = await catalog_snapshot(conn)
        status = classify(catalog, before, after)
        config = await config_snapshot(conn, allow_missing_marker=status == 'before')
    if status == 'before' and config[STATE_KEY]['exists'] and config[STATE_KEY]['value']['started']:
        raise ReleaseBlocked('started_before_schema')
    return status, catalog, config


async def apply_expansion(conn, *, expected_sha256, path=SQL_PATH):
    body = reviewed_body(path, expected_sha256)
    before, after = expected_catalogs()
    for attempt in range(3):
        try:
            async with asyncio.timeout(1):
                async with conn.transaction(isolation='read_committed'):
                    await conn.execute("SELECT set_config('search_path','',true),set_config('lock_timeout','1ms',true),"
                        "set_config('statement_timeout','1000ms',true),set_config('idle_in_transaction_session_timeout','1000ms',true)")
                    await conn.execute('LOCK TABLE public.messages,public.greetings,public.idempotency_keys,public.app_config '
                                       'IN ACCESS EXCLUSIVE MODE NOWAIT')
                    actual = await catalog_snapshot(conn)
                    status = classify(actual, before, after)
                    if status == 'before':
                        original = await config_snapshot(conn, allow_missing_marker=True)
                        if original[STATE_KEY]['exists'] and original[STATE_KEY]['value']['started']:
                            raise ReleaseBlocked('started_before_schema')
                        await conn.execute(body)
                        if await catalog_snapshot(conn) != after:
                            raise ReleaseBlocked('schema_postcondition_failed')
                    config = await config_snapshot(conn)
            return status == 'before', after, config
        except asyncpg.PostgresError as error:
            if error.sqlstate != '55P03' or attempt == 2:
                raise
            await asyncio.sleep(0.1 * (attempt + 1))


async def run(*, environment, expected_project_ref, expected_sql_sha256, expected_sha, apply=False):
    if (settings.environment != environment or settings.git_sha != expected_sha
            or not re.fullmatch(r'[0-9a-f]{40}', expected_sha)):
        raise ReleaseBlocked('runtime_environment_mismatch')
    dsn = settings.supabase_db_connection_string
    require_target(environment, expected_project_ref, dsn)
    reviewed_body(SQL_PATH, expected_sql_sha256)  # Validate inputs before connecting.
    dsn = dsn.replace('postgresql+asyncpg://', 'postgresql://', 1)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=3, command_timeout=2,
                                server_settings={'application_name': 'moly-chat-content-preflight'})
    try:
        status, catalog, config = await inspect_connection(conn)
        raw_mode = config[STATE_KEY]['value'] if config[STATE_KEY]['exists'] else {
            'required_key_ids': [1], 'active_key_id': 1}
        ring = initialize_content_crypto(required_key_ids=raw_mode['required_key_ids'])
        if ring is None:
            raise ReleaseBlocked('encryption_key_unavailable')
        owner = uuid.UUID(int=1)
        fingerprints = {}
        for key in raw_mode['required_key_ids']:
            body = b'local-bootstrap'
            cipher = seal(body, user_id=owner, table='messages', field='content', key_id=key,
                          aad_extra=('content_chars', len(body)))
            if open_bytes(cipher, user_id=owner, table='messages', field='content',
                          aad_extra=('content_chars', len(body))) != body:
                raise ReleaseBlocked('key_selftest_failed')
            fingerprints[str(key)] = hashlib.sha256(ring._keys[key]).hexdigest()
        applied = False
        if apply and status == 'before':
            # Three distinct bounded lock-only attempts; timeout/other failures
            # are not retried as if they were pure contention.
            applied, catalog, config = await apply_expansion(conn, expected_sha256=expected_sql_sha256)
            status = 'expanded'
        return {'format': FORMAT, 'environment': environment, 'project_ref': expected_project_ref,
                'version': expected_sha, 'key_fingerprints': fingerprints, 'self_test': 'ok',
                'sql_sha256': SQL_SHA256, 'schema_status': 'needs_expansion' if status == 'before' else status,
                'schema_fingerprint': digest(catalog), 'applied': applied, 'config_snapshot': config}
    finally:
        try:
            await asyncio.wait_for(conn.close(), timeout=0.25)
        except BaseException:
            conn.terminate()


async def main(args):
    value = await run(environment=args.environment, expected_project_ref=args.expected_project_ref,
        expected_sql_sha256=args.expected_sql_sha256, expected_sha=args.expected_sha, apply=args.apply)
    print(json.dumps(value, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', required=True, choices=tuple(PROJECTS))
    parser.add_argument('--expected-project-ref', required=True)
    parser.add_argument('--expected-sql-sha256', required=True)
    parser.add_argument('--expected-sha', required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true')
    group.add_argument('--apply', action='store_true')
    try:
        asyncio.run(main(parser.parse_args()))
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
