"""Read-only verification against the catalog generated from db/schema.sql.

Usage: python -m db.verify [--env dev|prod|/path/to/env] [--strict]
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.envfile import announce, load_conn  # noqa: E402
from db.schema_contract import verify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', default='dev')
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()
    dsn = load_conn(args.env)
    announce(args.env, dsn)
    try:
        problems = asyncio.run(verify(dsn, strict=args.strict))
    except Exception as exc:
        print(f'Schema verification failed ({type(exc).__name__})')
        return 1
    for problem in problems:
        print(problem)
    if not problems:
        print('Schema contract OK (columns, constraints, indexes, functions, triggers, RLS, grants)')
    return int(bool(problems))


if __name__ == '__main__':
    raise SystemExit(main())
