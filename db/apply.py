"""SQL 적용기. 기본 dry-run(실행 후 ROLLBACK). --commit 주면 실제 반영.

대상은 **기본 dev**(.env). 프로덕션은 `--env prod` 를 명시해야만 간다.
사용: python db/apply.py [경로=db/schema.sql] [--env dev|prod] [--commit]
"""
import asyncio
import asyncpg
import re
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])  # 레포 루트(스크립트 직접 실행 대비)
from db.envfile import announce, is_prod, load_conn, split_env_arg

async def main(commit: bool, path: str, env: str | None):
    sql = open(path).read()
    # 파일 자체 BEGIN/COMMIT 제거 — 우리가 트랜잭션 제어
    sql = re.sub(r'^\s*BEGIN;\s*$', '', sql, flags=re.M)
    sql = re.sub(r'^\s*COMMIT;\s*$', '', sql, flags=re.M)
    dsn = load_conn(env)
    announce(env, dsn, commit=commit)
    if commit and is_prod(env):  # 프로덕션 실반영은 한 번 더 눈에 띄게
        print(">>> PROD 실반영을 시작합니다. 의도한 것이 맞는지 확인하세요.", file=sys.stderr)
    c = await asyncpg.connect(dsn, statement_cache_size=0)
    tx = c.transaction()
    await tx.start()
    try:
        await c.execute(sql)
        # 검증: 생성된 테이블 수
        n = await c.fetchval("select count(*) from information_schema.tables where table_schema='public'")
        print(f"실행 성공. public 테이블 총 {n}개 (레거시 제거 후).")
        if commit:
            await tx.commit()
            print(">>> COMMIT 완료 — 실 DB 반영됨.")
        else:
            await tx.rollback()
            print(">>> DRY-RUN — ROLLBACK 완료(반영 안 됨). --commit 주면 실제 적용.")
    except Exception as e:
        await tx.rollback()
        print(f"!!! 실패 — ROLLBACK: {type(e).__name__}: {e}")
        raise
    finally:
        await c.close()

_env, _rest = split_env_arg(sys.argv[1:])
_args = [a for a in _rest if a != "--commit"]
_path = _args[0] if _args else "db/schema.sql"
asyncio.run(main("--commit" in _rest, _path, _env))
