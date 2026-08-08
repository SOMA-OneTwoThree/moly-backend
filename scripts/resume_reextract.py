"""중간에 끊긴 재추출을 이어서 돌린다 — **한 번에 몇 명씩만**.

재추출 잡을 한꺼번에 많이 걸면 DB 커넥션 풀이 못 버틴다. 2026-08-08에 250명을 한 번에
걸었다가 Supabase 풀러가 연결을 강제 종료하기 시작했고(`terminating connection due to
administrator command`) 앱 대화가 몇 분간 막혔다. 그래서 이 도구는 **소수만** 이어붙인다.

무엇을 하는가
    커서가 뒤처졌는데 대기·실행 중인 잡이 하나도 없는 사람에게 다음 구간 잡 하나를 건다.
    그 뒤는 잡 처리기가 알아서 이어간다(성공할 때마다 다음 구간 잡을 스스로 만든다).

⚠️ 앞 묶음이 **완전히 빈 뒤에** 다음을 건다. `--status`로 확인한다.

사용:
    uv run python scripts/resume_reextract.py --env prod --status
    uv run python scripts/resume_reextract.py --env prod --limit 10
    uv run python scripts/resume_reextract.py --env prod --limit 10 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import announce, load_conn, split_env_arg  # noqa: E402

# 이어붙일 대상 — 커서가 뒤처졌는데 잡이 하나도 없는 사람.
# 대화 중인 사람은 건너뛴다(그 사람의 잡은 채팅이 알아서 건다).
_STUCK = """
SELECT s.user_id, s.ingest_through_turn_seq AS cursor, s.repair_generation AS gen,
       s.privacy_epoch AS epoch, s.source_through_turn_seq AS src,
       (SELECT MIN(m.turn_seq) FROM messages m
         WHERE m.user_id = s.user_id AND m.kind = 'normal' AND m.turn_seq IS NOT NULL
           AND m.turn_seq > s.ingest_through_turn_seq
           AND m.turn_seq <= s.source_through_turn_seq) AS next_turn
FROM memory_pipeline_states s
WHERE s.mode <> 'legacy'
  AND s.bootstrap_status = 'ready'
  AND s.ingest_through_turn_seq < s.source_through_turn_seq
  AND NOT EXISTS (
    SELECT 1 FROM async_jobs j
    WHERE j.user_id = s.user_id AND j.job_type = 'mem0_ingest'
      AND j.state IN ('ready', 'running'))
  AND NOT EXISTS (
    SELECT 1 FROM messages m
    WHERE m.user_id = s.user_id AND m.created_at > now() - make_interval(mins => $2))
ORDER BY s.source_through_turn_seq - s.ingest_through_turn_seq DESC
LIMIT $1
"""

_ENQUEUE = """
INSERT INTO async_jobs
  (queue, job_type, user_id, dedup_key, payload, state, priority, available_at, max_attempts)
VALUES ('memory','mem0_ingest',$1,$2,$3::jsonb,'ready',500,now(),8)
ON CONFLICT (job_type, dedup_key) DO NOTHING
"""


async def show_status(c: asyncpg.Connection) -> None:
    r = await c.fetchrow("""
      SELECT (SELECT count(*) FROM memory_pipeline_states
               WHERE ingest_through_turn_seq < source_through_turn_seq) AS 끊긴사람,
             (SELECT count(*) FROM async_jobs WHERE queue='memory' AND state='ready') AS 대기잡,
             (SELECT count(*) FROM async_jobs WHERE queue='memory' AND state='running') AS 실행잡,
             (SELECT count(*) FROM async_jobs WHERE state='dead'
                AND created_at > now() - interval '12 hours') AS 죽은잡
    """)
    print("=== 현황 ===")
    for k, v in dict(r).items():
        print(f"  {k:<8} {v}")
    print("  ⚠️ 대기·실행 잡이 0이 된 뒤에 다음 묶음을 건다.")


async def resume(c: asyncpg.Connection, *, limit: int, idle_min: int, apply: bool) -> None:
    rows = await c.fetch(_STUCK, limit, idle_min)
    if not rows:
        print("이어붙일 사람 없음 — 전원 따라잡았거나 전부 대화 중이다.")
        return
    bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    print(f"=== 대상 {len(rows)}명 ({'실행' if apply else '미리보기'}) ===")
    made = 0
    for r in rows:
        uid, cursor, gen, epoch, src, nxt = (
            r["user_id"], r["cursor"], r["gen"], r["epoch"], r["src"], r["next_turn"])
        left = src - cursor
        print(f"  {str(uid)[:8]}  커서 {cursor}/{src} (남은 턴 {left}) 세대 {gen}", end="")
        if nxt is None:
            print("  → 건너뜀(처리할 턴 없음)")
            continue
        if not apply:
            print("  → [미리보기]")
            continue
        # 끝난 잡이 같은 키를 막고 있을 수 있으므로 시각을 넣어 유일하게 만든다.
        key = f"resume:{uid}:c{cursor}:g{gen}:{bucket}"
        res = await c.execute(
            _ENQUEUE, uid, key, f'{{"turn_seq": {nxt}, "privacy_epoch": {epoch}}}')
        ok = res.split()[-1] != "0"
        made += ok
        print(f"  → {'잡 등록' if ok else '⚠️ 키 충돌'}(turn {nxt})")
    if apply:
        print(f"\n{made}건 등록. 이 묶음이 다 빠진 뒤에 다음을 건다(--status).")


async def main(env: str | None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--status", action="store_true")
    p.add_argument("--limit", type=int, default=10, help="한 번에 이어붙일 인원 (작게 유지)")
    p.add_argument("--idle-min", type=int, default=10, help="이 시간 내 대화한 사람은 건너뛴다")
    p.add_argument("--apply", action="store_true", help="없으면 미리보기")
    a = p.parse_args(_rest)

    dsn = load_conn(env)
    announce(env, dsn, commit=a.apply)
    c = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        if a.status:
            await show_status(c)
        else:
            await resume(c, limit=a.limit, idle_min=a.idle_min, apply=a.apply)
            print()
            await show_status(c)
    finally:
        await c.close()


_env, _rest = split_env_arg(sys.argv[1:])
asyncio.run(main(_env))
