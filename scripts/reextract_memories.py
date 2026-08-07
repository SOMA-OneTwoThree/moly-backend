"""기억 전원 재추출 — 턴 단위로 뽑힌 옛 기억을 대화 덩어리 단위로 다시 만든다.

**지우지 않는다.** 옛 기억은 `semantic_status`를 회상에서 안 보이는 값으로 내리고
`classification_version`에 표시를 남긴다. 결과가 별로면 `--rollback`으로 그대로 되살린다.

한 사람씩 처리하는 이유
    그 사람만 재추출이 끝날 때까지 몇 분간 기억 없이 대화한다. 나머지는 평소대로 돈다.
    지금 대화 중인 사람은 건너뛴다(`--idle-min`).

어떻게 도는가
    1. 옛 기억을 숨긴다(표시를 남긴다)
    2. ingest·consolidated 커서를 0으로 되돌린다
    3. 첫 ingest 잡을 건다 — **그 뒤는 운영의 잡 처리기가 알아서 이어간다**
       (성공할 때마다 다음 구간 잡을 스스로 만든다)
    4. 커서가 따라잡으면 그 사람은 끝이다

    그래서 이 스크립트는 잡을 걸어두고 빠진다. 진행은 `--status`로 본다.

⚠️ 새 코드가 운영에 배포된 뒤에 돌려야 한다. 옛 코드가 처리하면 다시 턴 단위로 뽑는다.
⚠️ `post_deploy.sh`(좌표 없는 메시지에 턴 번호 매기기)를 **먼저** 끝내야 한다. 좌표가 없으면
   그 대화는 추출 대상에서 통째로 빠진다.

사용:
    MOLY_ENV_FILE=.env.prod uv run python scripts/reextract_memories.py --status
    MOLY_ENV_FILE=.env.prod uv run python scripts/reextract_memories.py --limit 3
    MOLY_ENV_FILE=.env.prod uv run python scripts/reextract_memories.py --limit 3 --apply
    MOLY_ENV_FILE=.env.prod uv run python scripts/reextract_memories.py --rollback --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import load_conn  # noqa: E402

# 되돌리기 표시 — 원래 상태를 값에 담아 두 가지를 구분해 되살린다.
MARK = {"active": "pre-reextract-active", "ambiguous": "pre-reextract-ambiguous"}
MARKS = tuple(MARK.values())

_STATUS = """
SELECT
  (SELECT count(*) FROM memory_pipeline_states WHERE mode='v2')                       AS v2_users,
  (SELECT count(*) FROM mem0_memory_registry WHERE semantic_status IN ('active','ambiguous')
      AND classification_version <> ALL($1::text[]))                                  AS visible_now,
  (SELECT count(*) FROM mem0_memory_registry WHERE classification_version = ANY($1::text[])) AS hidden_old,
  (SELECT count(*) FROM memory_pipeline_states s
     WHERE s.mode='v2' AND s.ingest_through_turn_seq < s.source_through_turn_seq)      AS in_progress
"""

# 재추출 대상 — 아직 안 건드렸고, 지금 대화 중이 아닌 사람부터.
_TARGETS = """
SELECT s.user_id,
       s.source_through_turn_seq AS turns,
       (SELECT count(*) FROM mem0_memory_registry r
         WHERE r.user_id = s.user_id AND r.semantic_status IN ('active','ambiguous')) AS mem
FROM memory_pipeline_states s
WHERE s.mode = 'v2'
  AND s.source_through_turn_seq >= $4                                 -- 최소 턴 수
  AND s.ingest_through_turn_seq >= s.source_through_turn_seq          -- 진행 중인 사람 제외
  AND NOT EXISTS (SELECT 1 FROM mem0_memory_registry r
                   WHERE r.user_id = s.user_id AND r.classification_version = ANY($1::text[]))
  AND NOT EXISTS (SELECT 1 FROM messages m
                   WHERE m.user_id = s.user_id
                     AND m.created_at > now() - make_interval(mins => $2))  -- 대화 중이면 건너뜀
ORDER BY s.source_through_turn_seq
LIMIT $3
"""


async def show_status(c: asyncpg.Connection) -> None:
    r = await c.fetchrow(_STATUS, list(MARKS))
    print("=== 현황 ===")
    print(f"  v2 사용자           {r['v2_users']}")
    print(f"  지금 보이는 기억     {r['visible_now']}")
    print(f"  숨긴 옛 기억         {r['hidden_old']}")
    print(f"  재추출 진행 중       {r['in_progress']}명")
    left = await c.fetchval(
        "SELECT count(*) FROM memory_pipeline_states s WHERE s.mode='v2' "
        "AND NOT EXISTS (SELECT 1 FROM mem0_memory_registry r WHERE r.user_id=s.user_id "
        "AND r.classification_version = ANY($1::text[]))", list(MARKS))
    print(f"  아직 안 한 사람       {left}")


async def reextract(
    c: asyncpg.Connection, *, limit: int, idle_min: int, apply: bool, min_turns: int
) -> None:
    rows = await c.fetch(_TARGETS, list(MARKS), idle_min, limit, min_turns)
    if not rows:
        print("대상 없음 — 전원 완료했거나 전부 대화 중이다.")
        return
    print(f"=== 대상 {len(rows)}명 ({'실행' if apply else '미리보기'}) ===")
    for r in rows:
        uid, turns, mem = r["user_id"], r["turns"], r["mem"]
        print(f"  {str(uid)[:8]}  턴 {turns:>4} · 기억 {mem:>4}건", end="")
        if not apply:
            print("  → [미리보기]")
            continue
        async with c.transaction():
            hidden = 0
            for orig, mark in MARK.items():
                n = await c.execute(
                    "UPDATE mem0_memory_registry SET semantic_status='superseded', "
                    "classification_version=$3, updated_at=now() "
                    "WHERE user_id=$1 AND semantic_status=$2", uid, orig, mark)
                hidden += int(n.split()[-1])
            # 커서를 되돌려 처음부터 다시 뽑게 한다. revision을 올리는 것이 핵심이다 —
            # 잡 멱등 키의 세대가 되어 **이미 처리한 turn을 다시 처리할 수 있게** 한다.
            gen = await c.fetchval(
                "UPDATE memory_pipeline_states SET ingest_through_turn_seq=0, "
                "consolidated_through_turn_seq=0, revision=revision+1, updated_at=now() "
                "WHERE user_id=$1 RETURNING revision", uid)
            # 첫 구간 잡만 건다 — 나머지는 처리기가 이어간다. 지연 없이 바로 돈다.
            first = await c.fetchval(
                "SELECT min(turn_seq) FROM messages WHERE user_id=$1 AND kind='normal' "
                "AND turn_seq IS NOT NULL", uid)
            if first is None:
                print("  → 건너뜀(좌표 있는 메시지 없음)")
                continue
            # max_attempts는 NOT NULL이고 기본값이 없다 — memory 큐 설정값(8)을 그대로 쓴다.
            # priority도 명시한다(기본 100). available_at은 지금 — 재추출은 기다릴 이유가 없다.
            #
            # ⚠️ 키에 **세대(revision)를 넣는다.** 안 넣으면 지난 백필이 만든 같은 키의 잡 행에
            #    막혀 `ON CONFLICT DO NOTHING`으로 조용히 무시된다(2026-08-08에 실제로 그랬다).
            key = f"mem0:{uid}:{first}:v1:{gen}"
            res = await c.execute(
                "INSERT INTO async_jobs "
                "  (queue, job_type, user_id, dedup_key, payload, state, priority, "
                "   available_at, max_attempts) "
                "VALUES ('memory','mem0_ingest',$1,$2,$3::jsonb,'ready',100,now(),8) "
                "ON CONFLICT (job_type, dedup_key) DO NOTHING",
                uid, key, f'{{"turn_seq": {first}, "privacy_epoch": 0}}')
            # **등록 결과를 반드시 확인한다.** 넣었다고 찍고 실제로는 안 들어가는 사고를 막는다.
            if res.split()[-1] == "0":
                raise SystemExit(f"잡 등록 실패(키 충돌): {key} — 중단한다")
        print(f"  → 숨김 {hidden}건 · 커서 0 · 세대 {gen} · 잡 등록(turn {first})")


async def rollback(c: asyncpg.Connection, *, apply: bool) -> None:
    n = await c.fetchval(
        "SELECT count(*) FROM mem0_memory_registry WHERE classification_version = ANY($1::text[])",
        list(MARKS))
    new = await c.fetchval(
        "SELECT count(*) FROM mem0_memory_registry r WHERE r.semantic_status IN ('active','ambiguous') "
        "AND r.classification_version <> ALL($1::text[]) AND EXISTS ("
        "  SELECT 1 FROM mem0_memory_registry o WHERE o.user_id=r.user_id "
        "  AND o.classification_version = ANY($1::text[]))", list(MARKS))
    print(f"되살릴 옛 기억 {n}건 · 숨길 새 기억 {new}건 ({'실행' if apply else '미리보기'})")
    if not apply:
        return
    async with c.transaction():
        for orig, mark in MARK.items():
            await c.execute(
                "UPDATE mem0_memory_registry SET semantic_status='superseded', updated_at=now() "
                "WHERE semantic_status IN ('active','ambiguous') "
                "AND classification_version <> ALL($1::text[]) "
                "AND user_id IN (SELECT user_id FROM mem0_memory_registry WHERE classification_version=$2)",
                list(MARKS), mark)
            await c.execute(
                "UPDATE mem0_memory_registry SET semantic_status=$2, "
                "classification_version='mem0-classifier-v2', updated_at=now() "
                "WHERE classification_version=$1", mark, orig)
        # ⚠️ **커서도 반드시 되돌린다.** 기억만 되살리고 커서를 0으로 두면 그 사용자의
        #    앞으로의 대화가 기억으로 안 들어간다 — chat이 "커서가 따라잡았을 때만" 잡을
        #    걸기 때문이다. 2026-08-08에 이걸 빠뜨려 3명이 그 상태로 남았다.
        n = await c.execute(
            "UPDATE memory_pipeline_states SET ingest_through_turn_seq=source_through_turn_seq, "
            "consolidated_through_turn_seq=source_through_turn_seq, revision=revision+1, "
            "updated_at=now() WHERE ingest_through_turn_seq < source_through_turn_seq")
        print("  커서 복구:", n)
    print("되돌리기 완료 — 옛 기억이 다시 회상에 나온다.")


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--status", action="store_true")
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--idle-min", type=int, default=10, help="이 시간 내 대화한 사람은 건너뛴다")
    p.add_argument("--min-turns", type=int, default=1, help="이 턴 수 이상인 사람만")
    p.add_argument("--apply", action="store_true", help="없으면 미리보기")
    a = p.parse_args()

    c = await asyncpg.connect(load_conn("prod"), statement_cache_size=0)
    try:
        if a.status:
            await show_status(c)
        elif a.rollback:
            await rollback(c, apply=a.apply)
        else:
            await reextract(c, limit=a.limit, idle_min=a.idle_min, apply=a.apply,
                            min_turns=a.min_turns)
            print()
            await show_status(c)
    finally:
        await c.close()


asyncio.run(main())
