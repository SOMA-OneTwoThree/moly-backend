"""기억 파이프라인 soak — 합성 부하로 SLO를 실측한다.

기억 재설계 13.6절 Dev 활성화 SLO. 10단계 read cutover 진입 전 실행한다.

**이 스크립트는 판정하지 않고 측정한다.** 통과/실패 선언은 `verify_cutover_gate.py`가 하고,
여기서는 그 판정에 필요한 지표를 만든다.

측정 지표(13.6절):
 · ingest+consolidation p50/p95 (job_attempts의 attempt 구간 — created_at→finished_at은
   queue wait·retry·handler를 섞으므로 쓰지 않는다)
 · attempt amplification (재시도 배수)
 · lease expiry / heartbeat 실패 수
 · cursor lag과 gap
 · purpose별 token/USD와 unknown_usage 비율
 · registry pending/orphan 잔여

⚠️ **실 provider 비용이 발생한다.** turn 하나당 extract 1회 + (기억이 있으면) consolidate 1회다.
`--turns`로 상한을 정하고, 예상 비용을 시작 전에 출력한다.

사용:
    PYTHONPATH=. uv run python scripts/soak_memory_pipeline.py --turns 20
    PYTHONPATH=. uv run python scripts/soak_memory_pipeline.py --turns 200 --minutes 360
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

import asyncpg

from db.envfile import announce, load_conn, split_env_arg

# catalog v1 기준 대략치. 정확한 값은 실행 후 ai_usage_ledger가 준다.
_EST_MICRO_USD_PER_TURN = 700


_METRICS = {
    "attempt_latency_ms": """
        SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
               percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
               max(duration_ms) AS max
        FROM job_attempts
        WHERE job_type LIKE 'mem0%' AND duration_ms IS NOT NULL
          AND started_at > $1""",
    "attempt_amplification": """
        SELECT count(*)::float / GREATEST(count(DISTINCT job_id), 1) AS ratio
        FROM job_attempts WHERE job_type LIKE 'mem0%' AND started_at > $1""",
    "lease_expiry": """
        SELECT count(*) FROM job_attempts
        WHERE outcome = 'lease_lost' AND started_at > $1""",
    "outcomes": """
        SELECT outcome, count(*) FROM job_attempts
        WHERE job_type LIKE 'mem0%' AND started_at > $1 GROUP BY 1 ORDER BY 2 DESC""",
    "cursor_lag": """
        SELECT max(source_through_turn_seq - ingest_through_turn_seq) AS ingest_lag,
               max(ingest_through_turn_seq - consolidated_through_turn_seq) AS consolidate_lag
        FROM memory_pipeline_states WHERE mode <> 'legacy'""",
    "registry": """
        SELECT semantic_status, count(*) FROM mem0_memory_registry GROUP BY 1 ORDER BY 2 DESC""",
    "usage": """
        SELECT purpose, count(*) AS calls, sum(cost_micro_usd) AS micro_usd,
               count(*) FILTER (WHERE status = 'unknown_usage') AS unknown
        FROM ai_usage_ledger WHERE started_at > $1 GROUP BY 1 ORDER BY 2 DESC""",
}


async def _enqueue_turns(conn, user_id, turns: int, started_key: str) -> int:
    """아직 처리되지 않은 turn들에 ingest 잡을 만든다. 이미 있으면 dedup으로 무시된다."""
    rows = await conn.fetch(
        """SELECT DISTINCT turn_seq FROM messages
           WHERE user_id=$1 AND kind='normal' AND turn_seq IS NOT NULL
           ORDER BY turn_seq LIMIT $2""",
        user_id, turns,
    )
    made = 0
    for r in rows:
        got = await conn.fetchval(
            """INSERT INTO async_jobs (queue, job_type, user_id, dedup_key, payload, max_attempts)
               VALUES ('content','mem0_ingest',$1,$2,$3::jsonb,3)
               ON CONFLICT (job_type, dedup_key) DO NOTHING RETURNING id""",
            user_id, f"soak:{started_key}:{r['turn_seq']}",
            f'{{"turn_seq": {r["turn_seq"]}, "privacy_epoch": 0}}',
        )
        if got is not None:
            made += 1
    return made


async def _report(conn, since) -> None:
    for name, sql in _METRICS.items():
        print(f"\n[{name}]")
        try:
            rows = await conn.fetch(sql, since) if "$1" in sql else await conn.fetch(sql)
        except Exception as e:  # noqa: BLE001
            print(f"  조회 실패: {type(e).__name__}")
            continue
        if not rows:
            print("  (없음)")
        for r in rows:
            print("  ", dict(r))


async def main(env: str | None, turns: int, minutes: int, yes: bool) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    est = turns * _EST_MICRO_USD_PER_TURN
    print(f"\n예상 비용: 약 {est} micro-USD (${est / 1_000_000:.4f}) — turn {turns}개 기준")
    print(f"관측 시간: 최대 {minutes}분")
    if not yes:
        print("\n⚠️ 실 provider 비용이 발생한다. 진행하려면 --yes 를 붙인다.")
        return 2

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        uid = await conn.fetchval(
            "SELECT user_id FROM memory_pipeline_states WHERE mode <> 'legacy' LIMIT 1"
        )
        if uid is None:
            print("\n❌ shadow/v2 사용자가 없다. 먼저 mode를 올려야 한다.")
            return 1
        since = await conn.fetchval("SELECT now()")
        key = str(int(time.time()))
        made = await _enqueue_turns(conn, uid, turns, key)
        print(f"\n잡 생성: {made}건 (대상 사용자 {str(uid)[:8]}…)")

        deadline = time.monotonic() + minutes * 60
        while time.monotonic() < deadline:
            pending = await conn.fetchval(
                """SELECT count(*) FROM async_jobs
                   WHERE job_type LIKE 'mem0%' AND state IN ('ready','running')"""
            )
            print(f"  대기/실행 중: {pending}", flush=True)
            if pending == 0:
                break
            await asyncio.sleep(20)

        print("\n" + "=" * 60)
        print("측정 결과 — 판정은 verify_cutover_gate.py가 한다")
        print("=" * 60)
        await _report(conn, since)
    finally:
        await conn.close()
    return 0


_env, _rest = split_env_arg(sys.argv[1:])
_p = argparse.ArgumentParser()
_p.add_argument("--turns", type=int, default=20)
_p.add_argument("--minutes", type=int, default=30)
_p.add_argument("--yes", action="store_true")
_a = _p.parse_args(_rest)
raise SystemExit(asyncio.run(main(_env, _a.turns, _a.minutes, _a.yes)))
