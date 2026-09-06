"""Real PostgreSQL checks, restricted to a disposable local moly_schema_* DB."""
from datetime import datetime, timedelta, timezone
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

from db.schema_contract import require_scratch
from db.schema_contract import load_contract
from db.catalog import compare_catalog, read_catalog


@pytest_asyncio.fixture
async def connection():
    dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('MOLY_SCHEMA_TEST_DSN is required for local schema tests')
    require_scratch(dsn)
    conn = await asyncpg.connect(dsn)
    tx = conn.transaction(isolation='repeatable_read')
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def signup(conn):
    uid = uuid.uuid4()
    created = datetime(2026, 9, 7, tzinfo=timezone.utc)
    await conn.execute('INSERT INTO auth.users(id,created_at) VALUES($1,$2)', uid, created)
    return uid, created


async def test_signup_preserves_gifts_trial_localized_routines_and_privacy_barrier(connection):
    uid, created = await signup(connection)
    profile = await connection.fetchrow('SELECT * FROM public.profiles WHERE id=$1', uid)
    assert profile['trial_ends_at'] == created + timedelta(hours=48)
    assert profile['hay_balance'] == 0
    # eedddd4 / 20260806: unspecified language defaults to English after overseas launch.
    assert profile['language'] == 'en'
    await connection.execute("UPDATE public.profiles SET language='ko-KR' WHERE id=$1", uid)
    assert await connection.fetchval('SELECT language FROM public.profiles WHERE id=$1', uid) == 'ko'
    assert await connection.fetchval(
        'SELECT count(*) FROM public.user_items WHERE user_id=$1', uid,
    ) == 2
    assert await connection.fetchval('''
        SELECT p.public_id FROM public.user_items i JOIN public.products p ON p.id=i.product_id
        WHERE i.user_id=$1 AND i.equipped_slot='theme'
    ''', uid) == 'theme_default'
    assert await connection.fetchval('''
        SELECT count(*) FROM public.routines WHERE user_id=$1
          AND name_i18n ?& ARRAY['ko','en','ja']
    ''', uid) == 2
    assert await connection.fetchval(
        "SELECT state FROM public.privacy_subject_barriers WHERE user_id=$1", uid,
    ) == 'active'
    await connection.execute('SELECT public.bootstrap_user($1,$2)', uid, created)
    assert await connection.fetchval(
        'SELECT count(*) FROM public.routines WHERE user_id=$1', uid,
    ) == 2


async def test_policy_nulls_remain_distinct_from_invalid_values(connection):
    uid, _ = await signup(connection)
    assert await connection.fetchval(
        "SELECT price_hay IS NULL FROM public.products WHERE public_id='theme_default'",
    )
    await connection.execute('UPDATE public.routines SET name_i18n=NULL WHERE user_id=$1', uid)
    async with connection.transaction():
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE public.routines SET name_i18n='null'::jsonb WHERE user_id=$1", uid,
                )
    with pytest.raises(asyncpg.CheckViolationError):
        async with connection.transaction():
            await connection.execute(
                "UPDATE public.products SET price_hay=0 WHERE public_id='theme_default'",
            )
    # Production intentionally does not forbid these two nullable fields on hay packs.
    await connection.execute('''
        UPDATE public.products SET public_id='nullable-policy-probe', asset_version=1
        WHERE app_store_product_id='com.geniusjun.moly.hay.300'
    ''')
    assert not await connection.fetchval(
        "SELECT is_active FROM public.products WHERE public_id='theme_workout'",
    )


async def test_account_delete_preserves_rpc_and_cascade_contract(connection):
    uid, _ = await signup(connection)
    await connection.execute('''
        INSERT INTO vecs.moly_memories_v2(id,vec,metadata)
        VALUES ('schema-smoke', array_fill(0::real, ARRAY[1536])::public.vector,
                jsonb_build_object('user_id',$1::text))
    ''', str(uid))
    assert await connection.fetchval('SELECT public.delete_user_memories($1)', uid) == 1
    assert await connection.fetchval('SELECT public.delete_user_memories($1)', uid) == 0
    await connection.execute('DELETE FROM auth.users WHERE id=$1', uid)
    for table in ['profiles', 'user_items', 'routines']:
        column = 'id' if table == 'profiles' else 'user_id'
        assert await connection.fetchval(
            f'SELECT count(*) FROM public.{table} WHERE {column}=$1', uid,
        ) == 0
    # The deletion fence deliberately outlives profiles (ERD account-deletion contract).
    assert await connection.fetchval(
        'SELECT count(*) FROM public.privacy_subject_barriers WHERE user_id=$1', uid,
    ) == 1


async def test_missing_required_catalog_fails_signup_atomically(connection):
    await connection.execute(
        "UPDATE public.products SET is_active=false WHERE public_id='theme_default'",
    )
    uid = uuid.uuid4()
    with pytest.raises(asyncpg.RaiseError):
        async with connection.transaction():
            await connection.execute('INSERT INTO auth.users(id) VALUES($1)', uid)
    assert await connection.fetchval('SELECT count(*) FROM auth.users WHERE id=$1', uid) == 0
    assert await connection.fetchval('SELECT count(*) FROM public.profiles WHERE id=$1', uid) == 0


@pytest.mark.parametrize(('sql', 'expected'), [
    ('DROP INDEX public.reward_ad_sessions_expiry_idx',
     'missing indexes: public.reward_ad_sessions_expiry_idx'),
    ('ALTER TABLE public.fortune_profiles DISABLE ROW LEVEL SECURITY',
     'changed tables: public.fortune_profiles'),
    ('GRANT SELECT ON public.fortune_profiles TO anon',
     'unexpected relation_grants: public.fortune_profiles:anon:SELECT'),
    ("ALTER TABLE public.profiles ALTER COLUMN language SET DEFAULT 'ko'",
     'changed columns: public.profiles.language'),
    ('ALTER TABLE public.profiles ALTER COLUMN timezone DROP NOT NULL',
     'changed columns: public.profiles.timezone'),
    ('ALTER TABLE auth.users DISABLE TRIGGER on_auth_user_created',
     'changed triggers: auth.users.on_auth_user_created'),
    ('CREATE POLICY drift_probe ON public.profiles USING (true)',
     'unexpected policies: public.profiles.drift_probe'),
    ('ALTER TABLE public.fortune_profiles ADD COLUMN drift_probe text NOT NULL',
     'unexpected columns: public.fortune_profiles.drift_probe'),
    ('CREATE UNIQUE INDEX drift_probe ON public.profiles(nickname)',
     'unexpected indexes: public.drift_probe'),
    ('CREATE TRIGGER drift_probe BEFORE INSERT ON public.profiles '
     'FOR EACH ROW EXECUTE FUNCTION public.normalize_profile_language()',
     'unexpected triggers: public.profiles.drift_probe'),
    ('ALTER FUNCTION public.bootstrap_user(uuid,timestamptz) SECURITY INVOKER',
     'changed functions: public.bootstrap_user(uuid,timestamp with time zone)'),
])
async def test_real_catalog_rejects_behavioral_and_security_drift(connection, sql, expected):
    await connection.execute(sql)
    problems = compare_catalog(load_contract(), await read_catalog(connection))
    assert expected in problems


async def test_additive_nullable_column_and_nonunique_index_allow_rollback(connection):
    await connection.execute('''
        ALTER TABLE public.profiles ADD COLUMN drift_probe text;
        CREATE INDEX drift_probe ON public.profiles(drift_probe);
    ''')
    actual = await read_catalog(connection)
    assert compare_catalog(load_contract(), actual) == []
    assert compare_catalog(load_contract(), actual, strict=True) == [
        'unexpected columns: public.profiles.drift_probe',
        'unexpected indexes: public.drift_probe',
    ]


@pytest.mark.parametrize('ending', ['COMMIT;', 'END;',
                                 "DO $$ BEGIN COMMIT; END $$;"])
async def test_reviewed_sql_cannot_commit_inside_default_rollback(
    connection, monkeypatch, tmp_path, ending,
):
    import db.apply as runner

    monkeypatch.setattr(runner, 'load_conn', lambda _: os.environ['MOLY_SCHEMA_TEST_DSN'])
    sql = tmp_path / 'reviewed.sql'
    # Inline transaction control bypasses simple whole-line BEGIN/COMMIT stripping.
    sql.write_text('CREATE TABLE public.apply_escape_probe(id int); ' + ending)
    with pytest.raises(asyncpg.PostgresError):
        await runner.apply(sql, 'local-schema-test')
    assert await connection.fetchval("SELECT to_regclass('public.apply_escape_probe')") is None


async def test_reviewed_sql_ddl_and_function_body_roll_back(connection, monkeypatch, tmp_path):
    import db.apply as runner

    monkeypatch.setattr(runner, 'load_conn', lambda _: os.environ['MOLY_SCHEMA_TEST_DSN'])
    sql = tmp_path / 'reviewed.sql'
    sql.write_text('''BEGIN;
CREATE TABLE public.apply_rollback_probe(id int);
CREATE FUNCTION public.apply_rollback_probe() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO public.apply_rollback_probe VALUES (1);
END;
$$;
SELECT public.apply_rollback_probe();
COMMIT;
''')
    await runner.apply(sql, 'local-schema-test')
    assert await connection.fetchval("SELECT to_regclass('public.apply_rollback_probe')") is None
    assert await connection.fetchval("SELECT to_regprocedure('public.apply_rollback_probe()')") is None
