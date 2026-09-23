"""Execute the price migration twice on dev PostgreSQL and roll it all back."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import assert_dev_target, load_conn  # noqa: E402


async def run(env_file):
    dsn = load_conn(env_file)
    assert_dev_target(env_file, dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=15, command_timeout=20)
    sql = (Path(__file__).resolve().parents[1] / 'db/changes/gpt6_price_catalog.sql').read_text()
    read = 'SELECT * FROM public.ai_price_catalog ORDER BY id'
    try:
        before = await conn.fetch(read)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SET LOCAL lock_timeout='2s'")
            await conn.execute(sql)
            first = await conn.fetch(read)
            await conn.execute(sql)
            assert first == await conn.fetch(read), 'Repeated migration changed prices'
            prior = {r['id']: dict(r) for r in before}
            assert all(dict(r) == prior[r['id']] for r in first if r['id'] in prior)
            assert len([r for r in first if r['catalog_version'] == 20260923]) == 4
            assert await conn.fetchval("""SELECT model FROM public.ai_price_catalog
                WHERE model='gpt-6-luna' AND effective_from<=now()
                ORDER BY effective_from DESC, catalog_version DESC LIMIT 1""") == 'gpt-6-luna'
            try:
                async with conn.transaction():
                    await conn.execute("UPDATE public.ai_price_catalog SET input_micro_usd=1 WHERE model='gpt-6-luna' AND catalog_version=20260923")
                    await conn.execute(sql)
            except asyncpg.RaiseError:
                pass
            else:
                raise AssertionError('Conflicting version was accepted')
            print('PASS: four rates, append-only history, repeat idempotency, conflicting-version rejection')
        finally:
            await tx.rollback()
        assert before == await conn.fetch(read), 'Public catalog changed after rollback'
        print('PASS: dev PostgreSQL rollback verified; no persistent catalog changes')
    finally:
        await conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', required=True)
    asyncio.run(run(parser.parse_args().env_file))
