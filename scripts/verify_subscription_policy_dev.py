"""Verify rollout SQL on the guarded dev DB, without changing public data/config.

Public DDL is dry-run and rolled back. Concurrent RPC tests use an empty, uniquely
named schema on the same dev PostgreSQL instance, removed in finally. Never prints
connection strings, user data or real offer codes. No local DB or paid APIs.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import assert_dev_target, load_conn  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FILES = ("subscription_launch.sql", "subscription_offer_expiry.sql", "subscription_test_access.sql", "subscription_policy_safety.sql")
FUNCTIONS = ("subscription_launch_config", "subscription_launch_access", "start_subscription_trial", "subscription_offer_status", "claim_subscription_offer")


def sql_file(name):
    return re.sub(r'^\s*(?:BEGIN|COMMIT);\s*$', '',
                  (ROOT / 'db/changes' / name).read_text(), flags=re.M | re.I)


async def check_public_ddl(conn, refresh_contract):
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='30s'")
        for name in FILES:
            await conn.execute(sql_file(name))
        await conn.execute("SET LOCAL search_path=''")
        rows = await conn.fetch("""
            SELECT p.oid::regprocedure::text AS key, pg_get_functiondef(p.oid) AS definition,
                   pg_get_userbyid(p.proowner) AS owner,
                   has_function_privilege('anon', p.oid, 'EXECUTE') AS anon,
                   has_function_privilege('authenticated', p.oid, 'EXECUTE') AS authenticated,
                   has_function_privilege('service_role', p.oid, 'EXECUTE') AS service
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' AND p.proname=ANY($1::text[])
        """, list(FUNCTIONS))
        assert len(rows) == len(FUNCTIONS)
        for row in rows:
            assert row['service'] and not row['anon'] and not row['authenticated']
        generated = {r['key']: {'definition': r['definition'], 'owner': r['owner']} for r in rows}
    finally:
        await tx.rollback()
    if refresh_contract:
        # A targeted, generated function refresh is valid only when the rest of the
        # baseline is byte-identical. Never replace main's catalog with dev's drift.
        base_schema = subprocess.check_output(['git', 'show', 'HEAD:db/schema.sql'], cwd=ROOT).decode()
        schema = (ROOT / 'db/schema.sql').read_text()
        def without_functions(value):
            for name in FUNCTIONS:
                value, count = re.subn(r'CREATE (?:OR REPLACE )?FUNCTION public\.' + name
                                      + r'\([\s\S]+?\n\$\$;', '', value)
                assert count >= 1
            return value
        assert without_functions(base_schema) == without_functions(schema)
        contract = json.loads(subprocess.check_output(
            ['git', 'show', 'HEAD:db/schema_contract.json'], cwd=ROOT))
        assert contract['schema_sha256'] == hashlib.sha256(base_schema.encode()).hexdigest()
        contract['catalog']['functions'].update(generated)
        contract['schema_sha256'] = hashlib.sha256(schema.encode()).hexdigest()
        (ROOT / 'db/schema_contract.json').write_text(
            json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    print('Public DDL + service-role-only RPC permissions: PASS (rolled back)')


async def check_isolated_rpcs(conn, peer):
    ns = 'subscription_verify_' + uuid.uuid4().hex
    created = False
    try:
        await conn.execute(f'CREATE SCHEMA {ns}')
        created = True
        for table in ('profiles', 'subscriptions', 'app_config'):
            await conn.execute(f'CREATE TABLE {ns}.{table} (LIKE public.{table} INCLUDING ALL)')
        await conn.execute(f'CREATE TABLE {ns}.auth_users (id uuid PRIMARY KEY, created_at timestamptz NOT NULL)')
        for name in FILES:
            await conn.execute(sql_file(name).replace('public.', f'{ns}.').replace('auth.users', f'{ns}.auth_users'))
        now = await conn.fetchval('SELECT clock_timestamp()')
        cutoff = now - timedelta(hours=1)
        rollout = {
            'enabled': True, 'campaign_id': 'isolated-test', 'apple_app_id': '12345',
            'existing_user_cutoff': cutoff.isoformat(),
            'legacy_offer_expires_at': (now + timedelta(days=7)).isoformat(),
            'offers': {platform: {plan: {
                'ready': True, 'product_id': f'test.{plan}', 'base_plan_id': plan,
                'offer_id': 'legacy-month',
            } for plan in ('monthly', 'yearly')} for platform in ('ios', 'android')},
        }
        async def config(value):
            await conn.execute(f"INSERT INTO {ns}.app_config(key,value) VALUES('subscription_launch',$1::jsonb) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", json.dumps(value))
        async def user(created_at):
            uid = uuid.uuid4()
            await conn.execute(f'INSERT INTO {ns}.auth_users VALUES($1,$2)', uid, created_at)
            await conn.execute(f"INSERT INTO {ns}.profiles(id,nickname,trial_ends_at) VALUES($1,'test',$2)",
                               uid, created_at + timedelta(hours=48))
            return uid
        async def start(db, uid):
            await db.execute(f'SELECT {ns}.start_subscription_trial($1)', uid)
        async def claim(db, uid, platform='ios', plan='monthly'):
            return json.loads(await db.fetchval(f'SELECT {ns}.claim_subscription_offer($1,$2,$3)', uid, platform, plan))
        async def rejected(awaitable):
            try:
                await awaitable
            except asyncpg.RaiseError:
                return
            raise AssertionError('Expected an unavailable trial/offer')
        async def count(table):
            return await conn.fetchval(f'SELECT count(*) FROM {ns}.{table}')

        old, old2 = await user(now - timedelta(days=30)), await user(now - timedelta(days=30))
        new = await user(cutoff + timedelta(minutes=1))
        for plan in ('monthly', 'yearly'):
            await conn.execute(f"INSERT INTO {ns}.subscription_trial_codes(campaign_id,plan,code,expires_at) "
                               "VALUES('isolated-test',$1,$2,$3)", plan, f'test-{plan}', now + timedelta(days=7))
        await config({**rollout, 'existing_user_cutoff': (now + timedelta(days=1)).isoformat()})
        await rejected(start(conn, new))
        await rejected(claim(conn, old))
        await config(rollout)
        await rejected(start(conn, old))
        await rejected(claim(conn, new))
        await asyncio.gather(start(conn, new), start(peer, new))
        row = await conn.fetchrow(f'SELECT app_trial_started_at, app_trial_ends_at FROM {ns}.profiles WHERE id=$1', new)
        assert row['app_trial_started_at'] == cutoff + timedelta(minutes=1)
        assert row['app_trial_ends_at'] - row['app_trial_started_at'] == timedelta(hours=48)
        # Same-account retries must return precisely the same allocated code.
        a, b = await asyncio.gather(claim(conn, old), claim(peer, old))
        assert a == b and await count('subscription_offer_claims') == 1
        assert await conn.fetchval(f'SELECT count(*) FROM {ns}.subscription_trial_codes WHERE claimed_at IS NOT NULL') == 1
        await rejected(claim(conn, old, plan='yearly'))
        await rejected(claim(conn, old2))  # Exhaustion never creates a partial claim.
        assert await count('subscription_offer_claims') == 1
        await rejected(claim(conn, old, platform=None))
        # A forced outer rollback must release both the claim and its selected code.
        await conn.execute(f"INSERT INTO {ns}.subscription_trial_codes(campaign_id,plan,code,expires_at) "
                           "VALUES('isolated-test','monthly','test-extra',$1)", now + timedelta(days=7))
        tx = conn.transaction()
        await tx.start()
        try:
            await claim(conn, old2)
        finally:
            await tx.rollback()
        assert await count('subscription_offer_claims') == 1
        assert await conn.fetchval(f"SELECT claimed_at IS NULL FROM {ns}.subscription_trial_codes WHERE code='test-extra'")
        # Shared lock serializes the RPC with the backend's financial write domain.
        await conn.execute('BEGIN')
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', str(old2))
        waiter = asyncio.create_task(claim(peer, old2))
        try:
            await asyncio.sleep(0.15)
            assert not waiter.done()
        finally:
            await conn.execute('ROLLBACK')
        await asyncio.wait_for(waiter, 5)
        await conn.execute(f'UPDATE {ns}.subscription_offer_claims SET redeemed_at=now() WHERE user_id=$1', old2)
        await rejected(claim(conn, old2))
        await config({**rollout, 'legacy_offer_expires_at': cutoff.isoformat()})
        await rejected(claim(conn, old))
        await config({**rollout, 'enabled': False})
        await start(conn, new)  # Issued interval survives deactivation and retries.
        assert dict(await conn.fetchrow(f'SELECT app_trial_started_at, app_trial_ends_at FROM {ns}.profiles WHERE id=$1', new)) == dict(row)
        # Execute the real webhook attribution SQL against isolated claims.
        from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
        from app.services.subscription import _mark_store_trial_offer_redeemed

        class AttributionSession:
            async def execute(self, statement):
                compiled = statement.compile(dialect=PGDialect_asyncpg())
                query = str(compiled).replace('public.', f'{ns}.')
                await conn.execute(query, *(compiled.params[k] for k in compiled.positiontup))

        async def redeemed():
            return await conn.fetchval(f'SELECT redeemed_at FROM {ns}.subscription_offer_claims WHERE user_id=$1', old)

        event = {'offer_code': 'legacy-month', 'product_id': 'test.monthly', 'store': 'APP_STORE'}
        await _mark_store_trial_offer_redeemed(AttributionSession(), old, {**event, 'offer_code': 'unrelated'})
        assert await redeemed() is None
        await _mark_store_trial_offer_redeemed(AttributionSession(), old, {**event, 'store': 'PLAY_STORE'})
        assert await redeemed() is None
        await _mark_store_trial_offer_redeemed(AttributionSession(), old, event)
        first = await redeemed()
        assert first is not None
        await _mark_store_trial_offer_redeemed(AttributionSession(), old, event)
        assert await redeemed() == first
        print('Actual PostgreSQL RPC boundaries, concurrent retries, exhaustion, shared lock, rollback, offer attribution: PASS')
    finally:
        if conn.is_in_transaction():
            await conn.execute('ROLLBACK')
        if created:
            await conn.execute(f'DROP SCHEMA {ns} CASCADE')
            print('Isolated dev test schema removed; public config/data unchanged')


async def run(env_file, refresh_contract):
    dsn = load_conn(env_file)
    assert_dev_target(env_file, dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=30)
    peer = None
    try:
        peer = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=15)
        await check_public_ddl(conn, refresh_contract)
        await check_isolated_rpcs(conn, peer)
    finally:
        await conn.close()
        if peer is not None:
            await peer.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--refresh-contract', action='store_true')
    args = parser.parse_args()
    asyncio.run(run(args.env_file, args.refresh_contract))
