"""현재 스키마와 수동 복구 지표를 읽기 전용으로 점검한다.

이전 이름은 운영 명령 호환을 위해 유지한다. schema.sql 구조 불일치는 실패한다.
데이터 지표는 정책·진행 중 작업·보관 기간 때문에 0이 아닐 수 있어 관찰값으로만 출력한다.
NULL이나 legacy 상태를 자동 수정하지 않는다.

    PYTHONPATH=. uv run python scripts/preflight_cutover.py --env dev
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.envfile import announce, load_conn, split_env_arg
from db.schema_contract import verify

OBSERVATIONS = [
    ('타 사용자 장부를 가리키는 기억 근거', 'SELECT count(*) FROM mem0_memory_sources s '
     'JOIN mem0_memory_registry r ON r.id=s.registry_id WHERE s.user_id<>r.user_id'),
    ('장부 없는 벡터 (진행 중 쓰기 포함)', 'SELECT count(*) FROM vecs.moly_memories_v2 v '
     'WHERE NOT EXISTS(SELECT 1 FROM mem0_memory_registry r WHERE r.provider_memory_id::text=v.id)'),
    ('30분 초과 planned 후보', "SELECT count(*) FROM mem0_ingest_candidates "
     "WHERE status='planned' AND created_at<now()-interval '30 minutes'"),
    ('비용 상한이 미확정인 unknown_usage', "SELECT count(*) FROM ai_usage_ledger "
     "WHERE status='unknown_usage' AND cost_upper_bound_micro_usd IS NULL"),
    ('timezone NULL (기존 Asia/Seoul 대체 정책)', 'SELECT count(*) FROM profiles WHERE timezone IS NULL'),
    ('좌표 없는 normal 메시지', "SELECT count(*) FROM messages WHERE kind='normal' AND turn_seq IS NULL"),
    ('미등록 또는 legacy 파이프라인', "SELECT count(*) FROM profiles p LEFT JOIN memory_pipeline_states s "
     "ON s.user_id=p.id WHERE COALESCE(s.mode,'legacy')='legacy'"),
    ('활성 사용자와 파이프라인의 개인정보 버전 차이', "SELECT count(*) FROM memory_pipeline_states s "
     "JOIN privacy_subject_barriers b USING(user_id) WHERE b.state='active' AND b.epoch<>s.privacy_epoch"),
    ('개인정보 장벽 없는 프로필', 'SELECT count(*) FROM profiles p WHERE NOT EXISTS '
     '(SELECT 1 FROM privacy_subject_barriers b WHERE b.user_id=p.id)'),
    ('미판정 기억', "SELECT count(*) FROM mem0_memory_registry WHERE semantic_status='pending'"),
    ('공급자 삭제 대기', "SELECT count(*) FROM mem0_memory_registry WHERE provider_delete_state='pending'"),
    ('기억 큐 dead 작업 (이력 포함)', "SELECT count(*) FROM async_jobs WHERE queue='memory' AND state='dead'"),
]


async def inspect(dsn: str) -> int:
    problems = await verify(dsn, strict=True)
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print('schema.sql 구조 일치')
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=30)
    try:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            for label, sql in OBSERVATIONS:
                print(f'  {label}: {int(await conn.fetchval(sql))}')
    finally:
        await conn.close()
    print('읽기 전용 점검 완료. 관찰값의 정상 여부는 이력과 현재 작업 상태를 함께 검토한다.')
    return 0


async def main(env: str | None) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    try:
        return await inspect(dsn)
    except Exception as exc:
        print(f'점검 실패 ({type(exc).__name__})')
        return 1


if __name__ == '__main__':
    env, rest = split_env_arg(sys.argv[1:])
    argparse.ArgumentParser(description=__doc__).parse_args(rest)
    raise SystemExit(asyncio.run(main(env)))
