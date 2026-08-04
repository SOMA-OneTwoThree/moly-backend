"""SQL 적용기. 기본 dry-run(실행 후 ROLLBACK). --commit 주면 실제 반영.

대상은 **기본 dev**(.env). 프로덕션은 `--env prod` 를 명시해야만 간다.
사용: python db/apply.py [경로=db/schema.sql] [--env dev|prod] [--commit]
"""
import asyncio
import asyncpg
import hashlib
from pathlib import Path
import re
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])  # 레포 루트(스크립트 직접 실행 대비)
from db.envfile import announce, assert_dev_target, load_conn, split_env_arg

async def main(commit: bool, path: str, env: str | None):
    sql = Path(path).read_text()
    # 파일 자체 BEGIN/COMMIT 제거 — 우리가 트랜잭션 제어
    sql = re.sub(r'^\s*BEGIN;\s*$', '', sql, flags=re.M)
    sql = re.sub(r'^\s*COMMIT;\s*$', '', sql, flags=re.M)
    dsn = load_conn(env)
    announce(env, dsn, commit=commit)
    if commit:
        assert_dev_target(env, dsn)
    c = await asyncpg.connect(dsn, statement_cache_size=0)
    tx = c.transaction()
    await tx.start()
    try:
        await c.execute(sql)
        checksum = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        migration_name = Path(path).name
        ledger = await c.fetchrow(
            "SELECT checksum_sha256 FROM public.schema_migrations WHERE migration_name=$1",
            migration_name,
        )
        if ledger is not None and ledger["checksum_sha256"] != checksum:
            raise RuntimeError(
                f"이미 적용된 migration checksum 불일치: {migration_name}"
            )
        if ledger is None:
            await c.execute(
                "INSERT INTO public.schema_migrations(migration_name,checksum_sha256) VALUES($1,$2)",
                migration_name,
                checksum,
            )
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
