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

import asyncpg

from db.envfile import announce, load_conn, split_env_arg

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
SELECT c.id, c.user_id, c.turn_seq, left(c.candidate_text, 40) AS txt
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

_ENQUEUE = """
INSERT INTO async_jobs (queue, job_type, user_id, dedup_key, payload, max_attempts)
VALUES ($1, $2, $3, $4, $5::jsonb, 3)
ON CONFLICT (job_type, dedup_key) DO NOTHING
RETURNING id
"""

_GENERATION = "SELECT repair_generation FROM memory_pipeline_states WHERE user_id=$1"


async def main(env: str | None, yes: bool) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    made = closed = 0
    try:
        print("\n[1] 판정 안 된 pending registry")
        for r in await conn.fetch(_PENDING_TURNS):
            uid, turn, n = r["user_id"], r["source_turn_seq"], r["n"]
            gen = int(await conn.fetchval(_GENERATION, uid) or 0)
            key = f"mem0c:{uid}:{turn}:v1:{gen}"
            print(f"  turn {turn}: {n}건 → consolidate 재큐 ({key})")
            if yes:
                got = await conn.fetchval(
                    _ENQUEUE, "content", "mem0_consolidate", uid, key,
                    f'{{"turn_seq": {turn}, "privacy_epoch": 0}}',
                )
                made += got is not None

        print("\n[2] provider delete backlog")
        for r in await conn.fetch(_DELETE_BACKLOG):
            uid, n = r["user_id"], r["n"]
            gen = int(await conn.fetchval(_GENERATION, uid) or 0)
            # 삭제 잡은 사용자 단위로 전부 훑으므로 turn은 아무 값이나 키 구분용이다.
            key = f"mem0d:{uid}:repair:{gen}"
            print(f"  {n}건 → delete 재큐 ({key})")
            if yes:
                got = await conn.fetchval(
                    _ENQUEUE, "maintenance", "mem0_provider_delete", uid, key,
                    '{"turn_seq": 0, "privacy_epoch": 0, "limit": 200}',
                )
                made += got is not None

        print("\n[3] 고아 planned 후보 (registry·벡터 둘 다 없음)")
        rows = await conn.fetch(_ORPHAN_PLANS)
        for r in rows:
            print(f"  turn {r['turn_seq']}: {r['txt']}")
        if yes and rows:
            closed = len(rows)
            await conn.execute(
                "UPDATE mem0_ingest_candidates SET status='dead', updated_at=now()"
                " WHERE id = ANY($1::uuid[])",
                [r["id"] for r in rows],
            )

        print("\n" + "=" * 56)
        if yes:
            print(f"잡 {made}건 생성, 고아 계획 {closed}건 dead 처리.")
            print("워커가 소진한 뒤 verify_cutover_gate.py 를 다시 돌린다.")
        else:
            print("dry-run이다. 실제 반영은 --yes 를 붙인다.")
    finally:
        await conn.close()
    return 0


_env, _rest = split_env_arg(sys.argv[1:])
_p = argparse.ArgumentParser()
_p.add_argument("--yes", action="store_true")
_a = _p.parse_args(_rest)
raise SystemExit(asyncio.run(main(_env, _a.yes)))
