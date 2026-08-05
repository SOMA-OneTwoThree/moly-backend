"""모델 ↔ 실 스키마 교차검증. 각 모델 컬럼이 DB에 있고 nullable/타입이 호환되는지.

대상은 **기본 dev**(.env). 프로덕션 검증은 `--env prod`.
사용: PYTHONPATH=. python db/verify.py [--env dev|prod]
"""
import asyncio
import asyncpg
import sys
import importlib
import pkgutil
import app.models as models_pkg
from app.core.db import Base
from db.envfile import announce, load_conn, split_env_arg

# 전 모델 임포트(Base.metadata 채우기)
for m in pkgutil.iter_modules(models_pkg.__path__):
    importlib.import_module(f"app.models.{m.name}")

async def main(env: str | None = None):
    dsn = load_conn(env)
    announce(env, dsn)
    c = await asyncpg.connect(dsn, statement_cache_size=0)
    db_cols = {}
    rows = await c.fetch("""select table_name, column_name, is_nullable, data_type
        from information_schema.columns where table_schema='public'""")
    for r in rows:
        db_cols.setdefault(r['table_name'], {})[r['column_name']] = (r['is_nullable'], r['data_type'])
    problems = []
    for table in Base.metadata.sorted_tables:
        tn = table.name
        if tn not in db_cols:
            problems.append(f"[테이블 없음] {tn}")
            continue
        for col in table.columns:
            if col.name not in db_cols[tn]:
                problems.append(f"[컬럼 없음] {tn}.{col.name}")
                continue
            db_null, db_type = db_cols[tn][col.name]
            # nullable 불일치(모델이 NOT NULL인데 DB가 nullable이면 위험)
            model_nullable = col.nullable
            db_nullable = (db_null == 'YES')
            if not model_nullable and db_nullable and not col.primary_key:
                problems.append(f"[nullable 불일치] {tn}.{col.name}: 모델 NOT NULL / DB NULL")
    # RLS 게이트: public 전 테이블 RLS ON + 민감 테이블(chat_contexts) anon/authenticated 권한 0.
    # (손으로 관리하는 RLS 배열이 새는 걸 방지 — EXP-7. 마이그레이션 적용 후 실행.)
    no_rls = await c.fetch("""select c.relname from pg_class c
        join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='public' and c.relkind='r' and not c.relrowsecurity""")
    for r in no_rls:
        problems.append(f"[RLS OFF] {r['relname']}")
    sensitive = ['chat_contexts', 'push_personalizations']  # 대화 파생 PII — REVOKE 회귀 감지
    grants = await c.fetch("""select table_name, grantee from information_schema.role_table_grants
        where table_schema='public' and table_name = any($1::text[])
          and grantee in ('anon','authenticated')""", sensitive)
    for r in grants:
        problems.append(f"[민감 테이블 권한 노출] {r['table_name']} → {r['grantee']}")

    print(f"모델 테이블 {len(Base.metadata.tables)}개 검증.")
    if problems:
        print("문제 발견:")
        for p in problems:
            print("  -", p)
    else:
        print("✅ 전 모델 컬럼이 DB에 존재. nullable/RLS/grant 위험 없음.")
    await c.close()

_env, _ = split_env_arg(sys.argv[1:])
asyncio.run(main(_env))
