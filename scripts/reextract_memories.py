"""기억 전원 재추출 — 턴 단위로 뽑힌 옛 기억을 대화 덩어리 단위로 다시 만든다.

**지우지 않는다.** 옛 기억은 `semantic_status`를 회상에서 안 보이는 값으로 내리고
`classification_version`에 표시를 남긴다. `--rollback`은 원본 벡터가 남아 있고 삭제 예정이 아닐 때만 표시된 기억을 복원한다.
롤백은 현재 source 커서까지 처리 완료로 맞춰 재추출 사슬을 중단하므로, 새 기억 결과와
미처리 구간을 함께 검토한 뒤 사용한다. 데이터베이스 전체의 시점 복구 기능은 아니다.

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

현행 이미지와 DB 구조를 검증한 뒤 사용한다. 실행 중인 기억 작업·삭제 중인 계정·epoch 불일치는
차단한다. 사용자별 트랜잭션이며 원문 메시지는 변경하지 않는다. 재추출 중에는 그 사용자의
기존 기억이 회상에서 숨겨지므로 운영자가 대상과 시점을 검토한 뒤 실행한다.

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
from db.maintenance import locked_subject, enqueue_recovery, cancel_ready_extraction, MaintenanceBlocked  # noqa: E402
from app.services.memory_pipeline import ingest_dedup_key  # noqa: E402

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
       p.language,
       s.source_through_turn_seq AS turns,
       (SELECT count(*) FROM mem0_memory_registry r
         WHERE r.user_id = s.user_id AND r.semantic_status IN ('active','ambiguous')) AS mem
FROM memory_pipeline_states s
JOIN profiles p ON p.id = s.user_id
JOIN privacy_subject_barriers b ON b.user_id=s.user_id AND b.state='active'
WHERE s.mode = 'v2' AND s.bootstrap_status='ready' AND s.privacy_epoch=b.epoch
  AND ($5::text IS NULL OR p.language = $5)                           -- 언어로 좁히기(선택)
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
    c: asyncpg.Connection, *, limit: int, idle_min: int, apply: bool, min_turns: int,
    language: str | None = None,
) -> None:
    rows = await c.fetch(_TARGETS, list(MARKS), idle_min, limit, min_turns, language)
    if not rows:
        print("대상 없음 — 전원 완료했거나 전부 대화 중이다.")
        return
    print(f"=== 대상 {len(rows)}명 ({'실행' if apply else '미리보기'}) ===")
    for r in rows:
        uid, turns, mem = r["user_id"], r["turns"], r["mem"]
        print(f"  {str(uid)[:8]} [{r['language']}] 턴 {turns:>4} · 기억 {mem:>4}건", end="")
        if not apply:
            print("  → [미리보기]")
            continue
        async with locked_subject(c, uid) as state:
            # Recheck eligibility under the same locks that protect the mutation.
            if state['ingest_through_turn_seq'] < state['source_through_turn_seq']:
                raise MaintenanceBlocked('pipeline_changed_since_preview')
            if await c.fetchval("SELECT EXISTS(SELECT 1 FROM messages WHERE user_id=$1 "
                                "AND created_at > now() - make_interval(mins => $2))", uid, idle_min):
                raise MaintenanceBlocked('subject_recently_active')
            if await c.fetchval("SELECT EXISTS(SELECT 1 FROM mem0_memory_registry WHERE user_id=$1 "
                                "AND classification_version=ANY($2::text[]))", uid, list(MARKS)):
                raise MaintenanceBlocked('reextract_already_started')
            first = await c.fetchval(
                "SELECT min(turn_seq) FROM messages WHERE user_id=$1 AND kind='normal' "
                "AND turn_seq IS NOT NULL AND turn_seq <= $2", uid, state['source_through_turn_seq'])
            if first is None:
                raise MaintenanceBlocked('source_turn_missing')
            epoch = state['privacy_epoch']
            await cancel_ready_extraction(c, uid)
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
            # Use the live key builder and job writer; lower priority keeps repairs
            # behind interactive memory work. A failure rolls back hiding + cursors.
            key = ingest_dedup_key(uid, 0, generation=gen)
            result = await enqueue_recovery(
                c, job_type='mem0_ingest', user_id=uid, dedup_key=key,
                payload={'turn_seq': first, 'privacy_epoch': epoch}, priority=500)
            if result is None:
                raise MaintenanceBlocked('reextract_job_not_created')
        print(f"  → 숨김 {hidden}건 · 커서 0 · 세대 {gen} · 잡 등록(turn {first})")


async def rollback(c: asyncpg.Connection, *, apply: bool, limit: int = 3) -> None:
    users = await c.fetch("SELECT DISTINCT user_id FROM mem0_memory_registry "
                          "WHERE classification_version=ANY($1::text[]) ORDER BY user_id LIMIT $2",
                          list(MARKS), limit)
    print(f"복구 후보 {len(users)}명 ({'실행' if apply else '미리보기'})")
    if not apply:
        return
    for user in users:
        uid = user['user_id']
        async with locked_subject(c, uid):
            # Historical rollback marks are usable only while their provider data
            # still exists. A removed vector must never be restored as visible.
            if await c.fetchval("""
                SELECT EXISTS(SELECT 1 FROM mem0_memory_registry r WHERE user_id=$1
                  AND classification_version=ANY($2::text[]) AND
                  (provider_delete_state <> 'kept' OR NOT EXISTS
                    (SELECT 1 FROM vecs.moly_memories_v2 v WHERE v.id=r.provider_memory_id::text)))
            """, uid, list(MARKS)):
                raise MaintenanceBlocked('original_vectors_unavailable')
            await cancel_ready_extraction(c, uid)
            # Hide all new visible rows before restoring either original status.
            # Otherwise the second pass hides the first pass's restored rows.
            await c.execute("UPDATE mem0_memory_registry SET semantic_status='superseded', updated_at=now() "
                            "WHERE user_id=$1 AND semantic_status IN ('active','ambiguous') "
                            "AND classification_version <> ALL($2::text[])", uid, list(MARKS))
            for original, mark in MARK.items():
                await c.execute("UPDATE mem0_memory_registry SET semantic_status=$2, "
                                "classification_version='mem0-classifier-v2', updated_at=now() "
                                "WHERE user_id=$3 AND classification_version=$1", mark, original, uid)
            await c.execute("UPDATE mem0_memory_registry SET semantic_status='excluded', updated_at=now() "
                            "WHERE user_id=$1 AND semantic_status='pending'", uid)
            # Preserve the existing operator rollback policy: stop the reextract
            # generation and let future turns progress from the current source.
            await c.execute("UPDATE memory_pipeline_states SET ingest_through_turn_seq=source_through_turn_seq, "
                            "consolidated_through_turn_seq=source_through_turn_seq, "
                            "repair_generation=repair_generation+1, revision=revision+1, updated_at=now() "
                            "WHERE user_id=$1", uid)
        print(f"  {str(uid)[:8]} 복구 완료")


async def main(env: str | None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--status", action="store_true")
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--idle-min", type=int, default=10, help="이 시간 내 대화한 사람은 건너뛴다")
    p.add_argument("--min-turns", type=int, default=1, help="이 턴 수 이상인 사람만")
    p.add_argument("--language", help="ko|ja|en — 이 언어 사용자만 (없으면 전체)")
    p.add_argument("--apply", action="store_true", help="없으면 미리보기")
    a = p.parse_args(_rest)

    if a.limit < 1 or a.idle_min < 0 or a.min_turns < 1:
        p.error('limit/min-turns must be positive; idle-min must be nonnegative')
    dsn = load_conn(env)
    announce(env, dsn, commit=a.apply)
    c = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        if a.status:
            await show_status(c)
        elif a.rollback:
            await rollback(c, apply=a.apply, limit=a.limit)
        else:
            await reextract(c, limit=a.limit, idle_min=a.idle_min, apply=a.apply,
                            min_turns=a.min_turns, language=a.language)
            print()
            await show_status(c)
    finally:
        await c.close()


if __name__ == '__main__':
    _env, _rest = split_env_arg(sys.argv[1:])
    asyncio.run(main(_env))
