"""배포 스큐로 죽은 기억 잡을 terminal 원본 보존 상태로 새 잡에 replay한다.

기본은 dry-run이다. `--execute`가 있어야 새 행을 만든다. payload 본문은 출력하지 않는다.
자동 대상은 원인이 확정된 `unknown_job_type`뿐이다. 다른 오류는 원인별 검토가 필요하다.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.config import settings
from app.services import jobs
from db.envfile import announce, assert_dev_target

_TARGETS_SQL = text("""
SELECT j.id
FROM async_jobs j
WHERE j.job_type IN ('memory_extract','memory_reconcile','memory_embed','relationship_profile_refresh')
  AND j.state='dead' AND j.last_error_code='unknown_job_type'
  AND NOT EXISTS (
    SELECT 1 FROM async_jobs r WHERE r.replay_of=j.id AND r.state IN ('ready','running','succeeded')
  )
ORDER BY j.created_at, j.id
""")


async def run(*, execute: bool) -> None:
    env_file = os.getenv("MOLY_ENV_FILE", ".env")
    announce(env_file, settings.supabase_db_connection_string, commit=execute)
    if execute:
        assert_dev_target(env_file, settings.supabase_db_connection_string)
    operation_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        ids = list((await session.execute(_TARGETS_SQL)).scalars().all())
        created = 0
        if execute:
            for job_id in ids:
                created += await jobs.replay_dead(
                    session, job_id=job_id, operation_id=operation_id
                ) is not None
            await session.commit()
    print(f"대상={len(ids)} replay={created if execute else 0}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="실제 replay 잡 생성")
    args = parser.parse_args()
    asyncio.run(run(execute=args.execute))


if __name__ == "__main__":
    main()
