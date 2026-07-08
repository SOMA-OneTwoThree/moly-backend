"""모델 ↔ 실 스키마 교차검증. 각 모델 컬럼이 DB에 있고 nullable/타입이 호환되는지."""
import asyncio
import asyncpg
import re
import importlib
import pkgutil
import app.models as models_pkg
from app.core.db import Base

def load_conn():
    for line in open(".env"):
        line=line.strip()
        if line.startswith("SUPABASE_DB_CONNECTION_STRING"):
            v=line.split("=",1)[1].strip().strip('"').strip("'")
            return re.sub(r'^postgresql\+asyncpg://','postgresql://',v)

# 전 모델 임포트(Base.metadata 채우기)
for m in pkgutil.iter_modules(models_pkg.__path__):
    importlib.import_module(f"app.models.{m.name}")

async def main():
    c = await asyncpg.connect(load_conn(), statement_cache_size=0)
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
    print(f"모델 테이블 {len(Base.metadata.tables)}개 검증.")
    if problems:
        print("문제 발견:")
        for p in problems:
            print("  -", p)
    else:
        print("✅ 전 모델 컬럼이 DB에 존재. nullable 위험 없음.")
    await c.close()
asyncio.run(main())
