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

기본은 dev다. 운영은 `--env prod` 를 붙여야만 선택된다.

사용:
    uv run python scripts/reextract_memories.py --status
    uv run python scripts/reextract_memories.py --env prod --status
    uv run python scripts/reextract_memories.py --env prod --limit 3
    uv run python scripts/reextract_memories.py --env prod --limit 3 --apply
    uv run python scripts/reextract_memories.py --env prod --rollback --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import announce, load_conn, split_env_arg  # noqa: E402

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
        # 첫 턴은 **아무것도 바꾸기 전에** 확인한다. 숨긴 뒤에 없는 걸 알면, 기억은 사라졌는데
        # 다시 뽑을 잡은 없는 상태로 커밋된다.
        first = await c.fetchval(
            "SELECT min(turn_seq) FROM messages WHERE user_id=$1 AND kind='normal' "
            "AND turn_seq IS NOT NULL", uid)
        if first is None:
            print("  → 건너뜀(좌표 있는 메시지 없음)")
            continue
        epoch = int(await c.fetchval(
            "SELECT privacy_epoch FROM memory_pipeline_states WHERE user_id=$1", uid) or 0)
        async with c.transaction():
            # 옛 세대의 남은 잡을 먼저 끊는다. 안 끊으면 그 잡이 뒤늦게 끝나면서 판정 커서를
            # 자기 턴까지 올려, 재추출 진행 중에 전환 관문 숫자가 실제보다 앞서 보인다.
            await c.execute(
                "UPDATE async_jobs SET state='cancelled', finished_at=now() "
                "WHERE user_id=$1 AND job_type IN ('mem0_ingest','mem0_consolidate') "
                "AND state IN ('ready','running')", uid)
            hidden = 0
            for orig, mark in MARK.items():
                n = await c.execute(
                    "UPDATE mem0_memory_registry SET semantic_status='superseded', "
                    "classification_version=$3, updated_at=now() "
                    "WHERE user_id=$1 AND semantic_status=$2", uid, orig, mark)
                hidden += int(n.split()[-1])
            # 커서를 되돌려 처음부터 다시 뽑게 한다. **`repair_generation`을 올리는 것이
            # 핵심이다** — 이 값 하나가 세 가지를 동시에 가른다.
            #   1. 잡 멱등 키(추출·판정·삭제 전부) — 이미 처리한 turn을 다시 처리할 수 있게 한다
            #   2. 후보 계획의 소유자 — 지난 세대가 남긴 후보를 "앞 시도의 것"으로 착각해
            #      추출을 건너뛰는 사고를 막는다(2026-08-08 실측: 4명 중 2명이 0건)
            #   3. provider id — 같은 턴에서 같은 말이 다시 나와도 새 행으로 들어간다
            # revision도 같이 올린다. 혹시 남아 있던 옛 판정 결과가 뒤늦게 반영되는 것을 막는다.
            gen = await c.fetchval(
                "UPDATE memory_pipeline_states SET ingest_through_turn_seq=0, "
                "consolidated_through_turn_seq=0, repair_generation=repair_generation+1, "
                "revision=revision+1, updated_at=now() "
                "WHERE user_id=$1 RETURNING repair_generation", uid)
            # 첫 구간 잡만 건다 — 나머지는 처리기가 이어간다. 지연 없이 바로 돈다.
            # max_attempts는 NOT NULL이고 기본값이 없다 — memory 큐 설정값(8)을 그대로 쓴다.
            #
            # ⚠️ priority를 **500으로 둔다.** 처리 순서가 `priority` 오름차순이라, 기본값(100)을
            #    쓰면 재추출 잡이 실사용자 잡보다 항상 먼저 처리된다. 실사용자 잡은 3분 뒤에
            #    도는데 재추출은 즉시라 정렬에서 더 밀린다. 574명 분량이 앞을 막으면 지금
            #    대화하는 사람의 기억이 큐 깊이만큼 늦어진다.
            #
            # ⚠️ 키에 **세대를 넣는다.** 안 넣으면 지난 백필이 만든 같은 키의 잡 행에
            #    막혀 `ON CONFLICT DO NOTHING`으로 조용히 무시된다(2026-08-08에 실제로 그랬다).
            #    형식은 `memory_pipeline.ingest_dedup_key`와 **글자 하나까지 같아야 한다** —
            #    다르면 이어지는 잡들과 키 공간이 갈려 같은 턴을 두 번 처리한다.
            #    기준은 처리 대상 턴이 아니라 **출발 커서**다. 위에서 커서를 0으로 내렸으니 0이다.
            key = f"mem0:{uid}:c0:v1:g{gen}"
            res = await c.execute(
                "INSERT INTO async_jobs "
                "  (queue, job_type, user_id, dedup_key, payload, state, priority, "
                "   available_at, max_attempts) "
                "VALUES ('memory','mem0_ingest',$1,$2,$3::jsonb,'ready',500,now(),8) "
                "ON CONFLICT (job_type, dedup_key) DO NOTHING",
                uid, key, f'{{"turn_seq": {first}, "privacy_epoch": {epoch}}}')
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
        # 되돌릴 사람을 **먼저 확정한다.** 아래에서 표시를 지우고 나면 누가 대상이었는지
        # 알 수 없다.
        uids = [r["user_id"] for r in await c.fetch(
            "SELECT DISTINCT user_id FROM mem0_memory_registry "
            "WHERE classification_version = ANY($1::text[])", list(MARKS))]
        if not uids:
            print("되돌릴 대상 없음.")
            return

        # ⚠️ **두 단계로 나눈다.** 예전에는 상태별(active/ambiguous)로 두 바퀴를 돌면서
        #    바퀴마다 "숨기고 되살리기"를 함께 했다. 그러면 **두 번째 바퀴가 첫 바퀴에서
        #    되살린 기억을 '새 기억'으로 보고 다시 숨긴다** — 33건이 2건이 됐다(2026-08-08).
        #    그래서 새 기억을 전부 숨긴 뒤에야 옛 기억을 되살린다.
        hidden = await c.execute(
            "UPDATE mem0_memory_registry SET semantic_status='superseded', updated_at=now() "
            "WHERE user_id = ANY($2::uuid[]) AND semantic_status IN ('active','ambiguous') "
            "AND classification_version <> ALL($1::text[])", list(MARKS), uids)
        restored = 0
        for orig, mark in MARK.items():
            r = await c.execute(
                "UPDATE mem0_memory_registry SET semantic_status=$2, "
                "classification_version='mem0-classifier-v2', updated_at=now() "
                "WHERE classification_version=$1", mark, orig)
            restored += int(r.split()[-1])

        # 남아 있는 재추출 잡을 취소한다. 안 그러면 되살린 직후에 다시 새 기억이 생긴다.
        #
        # **`running`도 함께 끊는다.** 대기 중인 것만 끊으면, 그때 돌고 있던 추출 잡이 끝나면서
        # 다음 구간 잡을 스스로 만들어 재추출이 그대로 이어진다. 상태를 바꾸면 그 잡의 마무리
        # UPDATE가 `state='running'` 조건에 걸려 실패하고, 그 잡이 한 일은 반영되지 않는다.
        #
        # **추출·판정 잡만** 끊는다. 큐 전체를 끊으면 채팅이 정상적으로 걸어둔 잡까지 죽는다.
        cancelled = await c.execute(
            "UPDATE async_jobs SET state='cancelled', finished_at=now() "
            "WHERE job_type IN ('mem0_ingest','mem0_consolidate') "
            "AND state IN ('ready','running') AND user_id = ANY($1::uuid[])", uids)
        # 판정 잡을 끊었으니 판정 못 받은 기억이 남는다. 회상은 `active`·`ambiguous`만 읽어
        # 사용자에게 보이지는 않지만, 그대로 두면 미판정으로 영원히 남아 운영 전환 관문을 막는다.
        left = await c.execute(
            "UPDATE mem0_memory_registry SET semantic_status='excluded', updated_at=now() "
            "WHERE user_id = ANY($1::uuid[]) AND semantic_status='pending'", uids)

        # ⚠️ **커서도 반드시 되돌린다.** 기억만 되살리고 커서를 0으로 두면 그 사용자의
        #    앞으로의 대화가 기억으로 안 들어간다 — chat이 "커서가 따라잡았을 때만" 잡을
        #    걸기 때문이다. 2026-08-08에 이걸 빠뜨려 3명이 그 상태로 남았다.
        #    **되돌린 사람만** 손댄다 — 조건만 보고 전체를 훑으면 지금 정상적으로 처리 중인
        #    사람의 커서까지 건너뛰어 그 대화가 기억에서 통째로 빠진다.
        #    ⚠️ 아직 추출 안 된 구간이 남아 있으면 그 대화는 기억으로 안 들어간다. 앞으로의
        #    대화가 통째로 막히는 것보다 낫다고 보고 이쪽을 택했다.
        cur = await c.execute(
            "UPDATE memory_pipeline_states SET ingest_through_turn_seq=source_through_turn_seq, "
            "consolidated_through_turn_seq=source_through_turn_seq, "
            "repair_generation=repair_generation+1, revision=revision+1, updated_at=now() "
            "WHERE user_id = ANY($1::uuid[])", uids)
        print(f"  대상 {len(uids)}명 · 새 기억 숨김 {hidden} · 옛 기억 복구 {restored}건")
        print(f"  잡 취소 {cancelled} · 미판정 정리 {left} · 커서 복구 {cur}")
    print("되돌리기 완료 — 옛 기억이 다시 회상에 나온다.")


async def main(env: str | None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--status", action="store_true")
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--idle-min", type=int, default=10, help="이 시간 내 대화한 사람은 건너뛴다")
    p.add_argument("--min-turns", type=int, default=1, help="이 턴 수 이상인 사람만")
    p.add_argument("--apply", action="store_true", help="없으면 미리보기")
    a = p.parse_args(_rest)

    dsn = load_conn(env)
    announce(env, dsn, commit=a.apply)
    c = await asyncpg.connect(dsn, statement_cache_size=0)
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


_env, _rest = split_env_arg(sys.argv[1:])
asyncio.run(main(_env))
