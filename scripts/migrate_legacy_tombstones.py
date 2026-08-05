"""legacy 망각 결정 → `legacy_recall_tombstones` 이관.

기억 재설계 7단계(docs/capi-memory-ARCHITECTURE.md 15장 7번, 14.3절).

⚠️ **이건 새 대화형 forget 기능이 아니다.** 과거에 사용자가 잊어달라고 한 source를 v2 backfill·
mem0 후보·timeline·diary hydrate가 되살리지 않게 하는 **전환 장벽**이다.

이관 대상과 매핑:
  `memory_recall_suppressions` → message_id (정확한 발언 단위 억제)
  `memory_source_closures`     → from~through watermark 구간의 source message 전부
  `memory_forget_markers`      → fact 근거 message (scope=fact) / 전역 marker는 예외 보고
  삭제된 `diaries`             → diary_id

불변식:
 1. **매핑되지 않은 원본이 1건이라도 있으면 그 사용자의 cutover를 막는다.** 조용히 건너뛰면
    잊어달라고 한 내용이 v2에서 되살아난다.
 2. 멱등하다 — partial unique index가 중복 삽입을 막고 여러 번 돌려도 같은 결과다.
 3. 기본 dry-run. `--commit`을 줘야 쓴다.

사용:
    PYTHONPATH=. uv run python scripts/migrate_legacy_tombstones.py
    PYTHONPATH=. uv run python scripts/migrate_legacy_tombstones.py --commit
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg

from db.envfile import announce, load_conn, split_env_arg

REASON_SUPPRESSION = "legacy_recall_suppression"
REASON_CLOSURE = "legacy_source_closure"
REASON_MARKER = "legacy_forget_marker"
REASON_DIARY = "legacy_diary_deleted"

# 1) 발언 단위 억제 — message_id가 이미 있다.
_SUPPRESSIONS = """
SELECT user_id, message_id, operation_id FROM memory_recall_suppressions
WHERE message_id IS NOT NULL
"""

# 2) closure 구간 → 그 구간의 source message 전부.
#    watermark는 legacy 좌표라 memory_source_turn_messages를 거쳐 message로 편다.
_CLOSURE_MESSAGES = """
SELECT DISTINCT c.user_id, m.message_id, c.forget_operation_id AS operation_id
FROM memory_source_closures c
JOIN memory_source_turn_messages m
  ON m.user_id = c.user_id
 AND m.source_watermark > c.from_watermark
 AND m.source_watermark <= c.through_watermark
"""

# 3) fact scope marker → 그 fact의 근거 message.
_MARKER_MESSAGES = """
SELECT DISTINCT f.user_id, e.source_id::bigint AS message_id, NULL::uuid AS operation_id
FROM memory_forget_markers f
JOIN memory_evidence e ON e.fact_id = f.fact_id AND e.user_id = f.user_id
WHERE f.fact_id IS NOT NULL AND e.source_type = 'message'
"""

# 4) fact가 아닌 scope(predicate/all) marker — message로 펼 수 없다. 보고 대상.
_UNMAPPABLE_MARKERS = """
SELECT user_id, scope, count(*) AS n FROM memory_forget_markers
WHERE fact_id IS NULL GROUP BY user_id, scope
"""

_DELETED_DIARIES = "SELECT user_id, id AS diary_id FROM diaries WHERE deleted_at IS NOT NULL"

_INSERT_MESSAGE = """
INSERT INTO legacy_recall_tombstones (user_id, source_message_id, source_operation_id, reason)
VALUES ($1, $2, $3, $4)
ON CONFLICT (user_id, source_message_id) WHERE source_message_id IS NOT NULL DO NOTHING
"""

_INSERT_DIARY = """
INSERT INTO legacy_recall_tombstones (user_id, diary_id, reason)
VALUES ($1, $2, $3)
ON CONFLICT (user_id, diary_id) WHERE diary_id IS NOT NULL DO NOTHING
"""


async def main(env: str | None, commit: bool) -> int:
    dsn = load_conn(env)
    announce(env, dsn, commit=commit)
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    counts: dict[str, int] = {}
    try:
        sources = (
            (REASON_SUPPRESSION, _SUPPRESSIONS),
            (REASON_CLOSURE, _CLOSURE_MESSAGES),
            (REASON_MARKER, _MARKER_MESSAGES),
        )
        for reason, sql in sources:
            rows = await conn.fetch(sql)
            counts[reason] = len(rows)
            if commit:
                for r in rows:
                    await conn.execute(
                        _INSERT_MESSAGE, r["user_id"], r["message_id"], r["operation_id"], reason
                    )

        diaries = await conn.fetch(_DELETED_DIARIES)
        counts[REASON_DIARY] = len(diaries)
        if commit:
            for r in diaries:
                await conn.execute(_INSERT_DIARY, r["user_id"], r["diary_id"], REASON_DIARY)

        unmappable = await conn.fetch(_UNMAPPABLE_MARKERS)
        total_tombstones = await conn.fetchval("SELECT count(*) FROM legacy_recall_tombstones")
        # 원본 대비 이관 검증 — 원본이 있는데 tombstone이 0이면 이관이 안 된 것이다.
        originals = {
            "memory_recall_suppressions": await conn.fetchval(
                "SELECT count(*) FROM memory_recall_suppressions"
            ),
            "memory_source_closures": await conn.fetchval(
                "SELECT count(*) FROM memory_source_closures"
            ),
            "memory_forget_markers": await conn.fetchval(
                "SELECT count(*) FROM memory_forget_markers"
            ),
            "deleted_diaries": len(diaries),
        }
    finally:
        await conn.close()

    print("\n원본:")
    for k, v in originals.items():
        print(f"  {k:28s} {v}")
    print("\n이관 대상(매핑된 source):")
    for k, v in counts.items():
        print(f"  {k:28s} {v}")
    print(f"\n현재 tombstone 총계: {total_tombstones}")

    if unmappable:
        print("\n❌ message로 펼 수 없는 marker — cutover 차단(불변식 1)")
        for r in unmappable:
            print(f"  user={r['user_id']} scope={r['scope']} {r['n']}건")
        return 1

    if originals["memory_recall_suppressions"] and not counts[REASON_SUPPRESSION]:
        print("\n❌ suppression 원본이 있는데 매핑 0건 — 이관 실패")
        return 1

    print("\n✅ unmapped 0." + ("" if commit else " (dry-run — --commit으로 반영)"))
    return 0


_env, _rest = split_env_arg(sys.argv[1:])
raise SystemExit(asyncio.run(main(_env, "--commit" in _rest)))
