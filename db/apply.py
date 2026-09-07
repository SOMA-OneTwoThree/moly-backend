"""Apply reviewed SQL in a bounded transaction; default mode rolls back.

Usage: python -m db.apply path.sql [--env dev|prod] [--commit] [--allow-prod]
Schema changes belong in schema.sql. This tool does not replay or accumulate migrations.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path
import re
import sys
import uuid

import asyncpg

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.envfile import announce, assert_dev_target, is_prod, load_conn  # noqa: E402


def reviewed_sql(path: Path, expected_sha256: str | None) -> str:
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError('SQL checksum differs from the reviewed file')
    # The enclosing transaction belongs to this tool, including baseline/seed files.
    sql = re.sub(r'^\s*(?:BEGIN|COMMIT);\s*$', '', content.decode(), flags=re.M | re.I)
    print(f'SQL: {path.name} | SHA-256: {digest}')
    return sql


async def apply(path: Path, env: str, *, commit: bool = False,
                allow_prod: bool = False, expected_sha256: str | None = None) -> None:
    sql = reviewed_sql(path, expected_sha256)
    dsn = load_conn(env)
    announce(env, dsn, commit=commit)
    if commit and allow_prod and not is_prod(env):
        raise ValueError('--allow-prod requires an explicit prod environment')
    if commit and not allow_prod:
        assert_dev_target(env, dsn)
    conn = await asyncpg.connect(
        dsn, statement_cache_size=0, timeout=10,
        server_settings={'application_name': 'moly-db-apply'},
    )
    transaction = conn.transaction()
    try:
        await transaction.start()
        await conn.execute("SET LOCAL lock_timeout = '2s'")
        await conn.execute("SET LOCAL statement_timeout = '60s'")
        await conn.execute("SET LOCAL idle_in_transaction_session_timeout = '60s'")
        # SPI forbids transaction control even inside nested DO/CALL statements.
        # Executing reviewed SQL this way keeps an inline COMMIT/END from escaping
        # our transaction and persisting writes during the default rollback run.
        body_tag = '$moly_body_' + uuid.uuid4().hex + '$'
        sql_tag = '$moly_sql_' + uuid.uuid4().hex + '$'
        await conn.execute(f'DO {body_tag} BEGIN EXECUTE {sql_tag}{sql}{sql_tag}; END {body_tag}')
        if commit:
            await transaction.commit()
            print('COMMIT completed')
        else:
            await transaction.rollback()
            print('DRY-RUN completed; ROLLBACK')
    except BaseException:
        if conn.is_in_transaction():
            await transaction.rollback()
        raise
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--env', default='dev')
    parser.add_argument('--commit', action='store_true')
    parser.add_argument('--allow-prod', action='store_true')
    parser.add_argument('--expected-sha256')
    args = parser.parse_args()
    try:
        asyncio.run(apply(args.path, args.env, commit=args.commit,
                          allow_prod=args.allow_prod, expected_sha256=args.expected_sha256))
    except Exception as exc:
        print(f'SQL apply failed ({type(exc).__name__}); transaction rolled back')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
