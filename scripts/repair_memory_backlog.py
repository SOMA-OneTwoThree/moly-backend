"""기억 파이프라인 잔여물 복구 — 커서가 지나가 버린 미완 작업을 다시 건다.

커서(`consolidated_through_turn_seq`)가 이미 지나간 turn은 정상 경로가 다시 방문하지 않는다.
그래서 아래 잔여물은 **스스로 사라지지 않고** cutover gate를 영구히 막는다:

 1. `pending` registry가 남은 turn — 판정 잡이 없어 영원히 미판정
 2. `provider_delete_state='pending'` — 정리 잡이 없어 벡터가 계속 과금
 3. 닫히지 않은 `planned` 후보 — registry도 벡터도 없는 고아 계획

1·2는 잡을 다시 걸어 정상 경로로 되돌린다. 3은 **provider에 벡터가 없는 것을 확인한 뒤에만**
`dead`로 닫는다 — 벡터가 있는데 계획만 닫으면 registry 없는 고아 벡터가 되어 더 나빠진다.

기본은 dry-run이다. 실제 반영은 `--yes`.

사용:
    PYTHONPATH=. uv run python scripts/repair_memory_backlog.py
    PYTHONPATH=. uv run python scripts/repair_memory_backlog.py --yes
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import announce, load_conn, split_env_arg  # noqa: E402
from db.maintenance import locked_subject, enqueue_recovery  # noqa: E402
from app.services.memory_pipeline import consolidate_dedup_key, provider_delete_dedup_key  # noqa: E402

_PENDING_TURNS = """
SELECT r.user_id, r.source_turn_seq, count(*) AS n
FROM mem0_memory_registry r
WHERE r.semantic_status = 'pending'
GROUP BY 1, 2 ORDER BY 1, 2
"""

_DELETE_BACKLOG = """
SELECT user_id, count(*) AS n FROM mem0_memory_registry
WHERE provider_delete_state = 'pending'
  AND semantic_status IN ('duplicate','superseded','excluded','rejected_policy')
GROUP BY 1
"""

# registry도 벡터도 없는 계획만 고아다. 하나라도 있으면 정상 경로가 처리할 수 있으니 건드리지 않는다.
_ORPHAN_PLANS = """
SELECT c.id, c.user_id, c.turn_seq
FROM mem0_ingest_candidates c
WHERE c.status = 'planned'
  AND c.created_at < now() - interval '30 minutes'
  AND NOT EXISTS (
    SELECT 1 FROM mem0_memory_registry r
    WHERE r.user_id = c.user_id AND r.provider_memory_id = c.provider_memory_id
  )
  AND NOT EXISTS (
    SELECT 1 FROM vecs.moly_memories_v2 v WHERE v.id = c.provider_memory_id::text
  )
ORDER BY c.created_at
"""

async def repair(conn: asyncpg.Connection, *, apply: bool, limit: int = 10) -> tuple[int, int]:
    made = closed = 0
    # Bound the operator batch. Never print memory text or messages.
    pending = await conn.fetch(_PENDING_TURNS + ' LIMIT $1', limit)
    deletion = await conn.fetch(_DELETE_BACKLOG + ' ORDER BY user_id LIMIT $1', limit)
    orphans = await conn.fetch(_ORPHAN_PLANS + ' LIMIT $1', limit)
    users = sorted({r['user_id'] for r in [*pending, *deletion, *orphans]})[:limit]
    for uid in users:
        print(f"  {str(uid)[:8]} 복구 후보 ({'실행' if apply else '미리보기'})")
        if not apply:
            continue
        async with locked_subject(conn, uid) as state:
            epoch, gen = state['privacy_epoch'], state['repair_generation']
            for row in pending:
                if row['user_id'] != uid:
                    continue
                turn = row['source_turn_seq']
                if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM mem0_memory_registry "
                                           "WHERE user_id=$1 AND source_turn_seq=$2 "
                                           "AND semantic_status='pending')", uid, turn):
                    continue
                made += await enqueue_recovery(
                    conn, job_type='mem0_consolidate', user_id=uid,
                    dedup_key=consolidate_dedup_key(uid, turn, generation=gen),
                    payload={'turn_seq': turn, 'privacy_epoch': epoch}) is not None
            if any(row['user_id'] == uid for row in deletion):
                made += await enqueue_recovery(
                    conn, job_type='mem0_provider_delete', user_id=uid,
                    dedup_key=provider_delete_dedup_key(uid, 0, generation=gen),
                    payload={'turn_seq': 0, 'privacy_epoch': epoch, 'limit': 50}) is not None
            ids = [row['id'] for row in orphans if row['user_id'] == uid]
            if ids:
                # Recheck all absence conditions in the write statement; the
                # preview result alone is not authority to close a candidate.
                changed = await conn.execute("""
                    UPDATE mem0_ingest_candidates c SET status='dead', updated_at=now()
                    WHERE c.id=ANY($1::uuid[]) AND c.user_id=$2 AND c.status='planned'
                      AND c.created_at < now() - interval '30 minutes'
                      AND NOT EXISTS (SELECT 1 FROM mem0_memory_registry r
                        WHERE r.user_id=c.user_id AND r.provider_memory_id=c.provider_memory_id)
                      AND NOT EXISTS (SELECT 1 FROM vecs.moly_memories_v2 v
                        WHERE v.id=c.provider_memory_id::text)
                """, ids, uid)
                closed += int(changed.split()[-1])
    return made, closed


async def main(env: str | None, yes: bool, limit: int = 10) -> int:
    dsn = load_conn(env)
    announce(env, dsn, commit=yes)
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        made, closed = await repair(conn, apply=yes, limit=limit)
        print(f"잡 {made}건 생성, 고아 계획 {closed}건 dead 처리.")
        if not yes:
            print('미리보기: 변경 없음. 검토 후 --yes로 반영한다.')
    finally:
        await conn.close()
    return 0


if __name__ == '__main__':
    _env, _rest = split_env_arg(sys.argv[1:])
    _p = argparse.ArgumentParser()
    _p.add_argument('--yes', action='store_true')
    _p.add_argument('--limit', type=int, default=10)
    _a = _p.parse_args(_rest)
    if _a.limit < 1:
        _p.error('limit must be positive')
    raise SystemExit(asyncio.run(main(_env, _a.yes, _a.limit)))
