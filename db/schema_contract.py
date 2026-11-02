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
    parser.add_argument('--env', default='dev')
    parser.add_argument('--expected-project-ref')
    parser.add_argument('--reviewed-sql', type=Path)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--baseline-schema', type=Path)
    args = parser.parse_args()
    raw = os.environ.get('SUPABASE_DB_CONNECTION_STRING', '')
    if not raw and not args.refresh_subscription_from_dev:
        parser.error('SUPABASE_DB_CONNECTION_STRING is required')
    dsn = re.sub(r'^postgresql\+asyncpg://', 'postgresql://', raw)
    try:
        if args.refresh_subscription_from_dev:
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
        print('Schema contract OK')
    return int(bool(problems))


if __name__ == '__main__':
    raise SystemExit(main())
