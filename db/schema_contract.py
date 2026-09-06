"""Generated catalog contract for schema.sql; production checks are read-only."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse

import asyncpg

from db.catalog import compare_catalog, read_catalog

SCHEMA = Path(__file__).with_name('schema.sql')
CONTRACT = Path(__file__).with_name('schema_contract.json')


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
    args = parser.parse_args()
    raw = os.environ.get('SUPABASE_DB_CONNECTION_STRING', '')
    if not raw:
        parser.error('SUPABASE_DB_CONNECTION_STRING is required')
    dsn = re.sub(r'^postgresql\+asyncpg://', 'postgresql://', raw)
    try:
        if args.generate or args.check_generated:
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
