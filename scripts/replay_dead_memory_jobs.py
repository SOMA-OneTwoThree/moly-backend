"""현재 기억 처리기가 배포 스큐로 거부한 dead 잡을 이력 보존 상태로 replay한다.

원인이 unknown_job_type인 현재 mem0 잡만 제한된 수로 검토한다. 폐기된 처리기 이름을
새 이름으로 변환하지 않는다. 기본 미리보기, --execute는 기존처럼 개발 DB만 허용한다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import announce, assert_dev_target, load_conn, split_env_arg  # noqa: E402
from db.maintenance import MaintenanceBlocked, enqueue_recovery, locked_subject  # noqa: E402
from app.services.memory_pipeline import consolidate_dedup_key, provider_delete_dedup_key  # noqa: E402

_TARGETS = """
SELECT id,user_id,job_type,dedup_key,payload FROM async_jobs j
WHERE job_type IN ('mem0_ingest','mem0_consolidate','mem0_provider_delete')
  AND state='dead' AND last_error_code='unknown_job_type' AND user_id IS NOT NULL
  AND replay_of IS NULL AND payload_redacted_at IS NULL
  AND (expires_at IS NULL OR expires_at>now())
  AND (payload_expires_at IS NULL OR payload_expires_at>now())
  AND NOT EXISTS(SELECT 1 FROM async_jobs r WHERE r.replay_of=j.id)
ORDER BY created_at,id LIMIT $1
"""


async def replay(conn, *, execute: bool, limit: int) -> int:
    rows = await conn.fetch(_TARGETS, limit)
    made = 0
    if execute:
        for row in rows:
            async with locked_subject(conn, row['user_id']) as state:
                payload = json.loads(row['payload'])
                if payload.get('privacy_epoch') != state['privacy_epoch']:
                    raise MaintenanceBlocked('privacy_epoch_mismatch')
                gen = state['repair_generation']
                key = row['dedup_key']
                turn = payload.get('turn_seq')
                if type(turn) is not int or turn < 0:
                    raise MaintenanceBlocked('invalid_turn_payload')
                if row['job_type'] == 'mem0_ingest':
                    match = re.fullmatch(re.escape(f"mem0:{row['user_id']}:c") +
                                         r'([0-9]+):v1(?::g([0-9]+))?', key)
                    valid = (match is not None and int(match[2] or 0) == gen
                             and int(match[1]) == state['ingest_through_turn_seq'])
                else:
                    builder = (consolidate_dedup_key if row['job_type'] == 'mem0_consolidate'
                               else provider_delete_dedup_key)
                    valid = key == builder(row['user_id'], turn, generation=gen)
                if not valid:
                    raise MaintenanceBlocked('repair_coordinate_mismatch')
                made += await enqueue_recovery(
                    conn, job_type=row['job_type'], user_id=row['user_id'],
                    dedup_key=key, payload=payload) is not None
    print(f'대상={len(rows)} replay={made}')
    return made


async def run(env: str | None, *, execute: bool, limit: int) -> None:
    dsn = load_conn(env)
    announce(env, dsn, commit=execute)
    if execute:
        assert_dev_target(env, dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        await replay(conn, execute=execute, limit=limit)
    finally:
        await conn.close()


if __name__ == '__main__':
    env, rest = split_env_arg(sys.argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args(rest)
    if args.limit < 1:
        parser.error('limit must be positive')
    asyncio.run(run(env, execute=args.execute, limit=args.limit))
