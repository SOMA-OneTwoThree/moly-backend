"""SQL 적용기. 기본 dry-run(실행 후 ROLLBACK). --commit 주면 실제 반영.
사용: python db/apply.py [경로=db/schema.sql] [--commit]"""
import asyncio, asyncpg, re, sys

def load_conn():
    for line in open(".env"):
        line=line.strip()
        if line.startswith("SUPABASE_DB_CONNECTION_STRING"):
            v=line.split("=",1)[1].strip().strip('"').strip("'")
            return re.sub(r'^postgresql\+asyncpg://','postgresql://',v)
    raise SystemExit("no conn")

async def main(commit: bool, path: str):
    sql = open(path).read()
    # 파일 자체 BEGIN/COMMIT 제거 — 우리가 트랜잭션 제어
    sql = re.sub(r'^\s*BEGIN;\s*$', '', sql, flags=re.M)
    sql = re.sub(r'^\s*COMMIT;\s*$', '', sql, flags=re.M)
    c = await asyncpg.connect(load_conn(), statement_cache_size=0)
    tx = c.transaction()
    await tx.start()
    try:
        await c.execute(sql)
        # 검증: 생성된 테이블 수
        n = await c.fetchval("select count(*) from information_schema.tables where table_schema='public'")
        print(f"실행 성공. public 테이블 총 {n}개 (레거시 제거 후).")
        if commit:
            await tx.commit(); print(">>> COMMIT 완료 — 실 DB 반영됨.")
        else:
            await tx.rollback(); print(">>> DRY-RUN — ROLLBACK 완료(반영 안 됨). --commit 주면 실제 적용.")
    except Exception as e:
        await tx.rollback()
        print(f"!!! 실패 — ROLLBACK: {type(e).__name__}: {e}")
        raise
    finally:
        await c.close()

_args = [a for a in sys.argv[1:] if a != "--commit"]
_path = _args[0] if _args else "db/schema.sql"
asyncio.run(main("--commit" in sys.argv, _path))
