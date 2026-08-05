"""read cutover gate 판정 — 10단계 진입 전 실측 검사.

기억 재설계(docs/capi-memory-ARCHITECTURE.md 15장 read cutover gate).

**DB로 판정 가능한 항목만 여기서 본다.** 부하·시간이 필요한 항목(6시간 soak, provider 장애 drain,
invoice 대사)은 판정하지 않고 `수동`으로 표시한다 — 통과했다고 자동으로 말하지 않는 게 이 도구의
핵심이다. 자동 항목이 전부 통과해도 cutover 조건을 만족한 게 아니다.

사용:
    PYTHONPATH=. uv run python scripts/verify_cutover_gate.py
    PYTHONPATH=. uv run python scripts/verify_cutover_gate.py --env prod
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg

from db.envfile import announce, load_conn, split_env_arg

# (이름, SQL, 기대값). 기대값 0 = "이 조건에 걸리는 행이 하나도 없어야 한다".
_CHECKS: list[tuple[str, str]] = [
    (
        "ingest 커서가 source를 따라잡지 못한 사용자",
        """SELECT count(*) FROM memory_pipeline_states
           WHERE mode <> 'legacy' AND ingest_through_turn_seq <> source_through_turn_seq""",
    ),
    (
        "consolidation 커서가 ingest를 따라잡지 못한 사용자",
        """SELECT count(*) FROM memory_pipeline_states
           WHERE mode <> 'legacy' AND consolidated_through_turn_seq <> ingest_through_turn_seq""",
    ),
    (
        "registry pending 잔여",
        "SELECT count(*) FROM mem0_memory_registry WHERE semantic_status='pending'",
    ),
    (
        "provider delete backlog",
        "SELECT count(*) FROM mem0_memory_registry WHERE provider_delete_state='pending'",
    ),
    (
        "닫히지 않은 planned 후보(30분 초과)",
        """SELECT count(*) FROM mem0_ingest_candidates
           WHERE status='planned' AND created_at < now() - interval '30 minutes'""",
    ),
    (
        "registry 없는 벡터(고아)",
        """SELECT count(*) FROM vecs.moly_memories_v2 v
           WHERE NOT EXISTS (
             SELECT 1 FROM mem0_memory_registry r
             WHERE r.provider_memory_id::text = v.id
           )""",
    ),
    (
        "payload user_id가 없는 벡터",
        "SELECT count(*) FROM vecs.moly_memories_v2 WHERE metadata->>'user_id' IS NULL",
    ),
    (
        "source edge가 타 사용자를 가리키는 registry",
        """SELECT count(*) FROM mem0_memory_sources s
           JOIN mem0_memory_registry r ON r.id = s.registry_id
           WHERE s.user_id <> r.user_id""",
    ),
    (
        "assistant 발화를 근거로 삼은 source edge",
        "SELECT count(*) FROM mem0_memory_sources WHERE source_sender <> 'user'",
    ),
    (
        "tombstone source가 v2 기억으로 재노출",
        """SELECT count(*) FROM mem0_memory_sources s
           JOIN legacy_recall_tombstones t
             ON t.user_id = s.user_id AND t.source_message_id = s.source_message_id
           JOIN mem0_memory_registry r ON r.id = s.registry_id
           WHERE r.semantic_status IN ('active','ambiguous')""",
    ),
    (
        "장벽 없는 profile(enforced 전환 차단 조건)",
        """SELECT count(*) FROM profiles p
           WHERE NOT EXISTS (
             SELECT 1 FROM privacy_subject_barriers b WHERE b.user_id = p.id
           )""",
    ),
    (
        "retryable dead 잡",
        "SELECT count(*) FROM async_jobs WHERE state='dead' AND job_type LIKE 'mem0%'",
    ),
    (
        "미수렴 원장 행(started로 방치)",
        """SELECT count(*) FROM ai_usage_ledger
           WHERE status='started' AND started_at < now() - interval '15 minutes'""",
    ),
    (
        "상한 추정 없는 unknown_usage",
        """SELECT count(*) FROM ai_usage_ledger
           WHERE status='unknown_usage' AND cost_upper_bound_micro_usd IS NULL""",
    ),
]

# DB만으로 판정할 수 없는 항목 — 통과 여부를 자동으로 말하지 않는다.
_MANUAL: list[str] = [
    "production-like 구간의 ledger 99.9% 수렴 (dev 트래픽으로는 모수 부족)",
    "최신 price catalog와 실제 invoice 3% 이내 대사 (청구서 필요)",
    "provider 10분 장애 뒤 backlog 완전 drain (fault injection 필요)",
    "6시간 synthetic soak (시간 필요)",
    "연속 두 sweep에서 gate 유지 (반복 실행 필요)",
    # 2026-08-05 dev 실측: 올바른 배치 cached=2170 / 금지 배치 cached=0 로 통과.
    # 프롬프트 구성이 바뀌면 다시 돌려야 하므로 자동 항목으로 올리지 않는다.
    "cache fixture 재실행 (scripts/verify_prompt_cache.py --yes — 프롬프트 구성 변경 시마다)",
    "golden recall 기준 통과 (golden set 고정·평가 필요)",
    "09:00/20:00 알림 expiry·dedup fixture",
]


async def main(env: str | None) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    failures: list[tuple[str, int]] = []
    try:
        print("\n[자동 판정]")
        for name, sql in _CHECKS:
            try:
                got = int(await conn.fetchval(sql) or 0)
            except Exception as e:  # noqa: BLE001  테이블 미존재 등도 게이트 실패로 본다
                print(f"  ⚠️  {name}: 조회 실패 — {type(e).__name__}")
                failures.append((name, -1))
                continue
            mark = "✅" if got == 0 else "❌"
            print(f"  {mark} {name}: {got}")
            if got != 0:
                failures.append((name, got))
    finally:
        await conn.close()

    print("\n[수동 판정 — 이 도구가 통과를 선언하지 않는다]")
    for item in _MANUAL:
        print(f"  ⏸  {item}")

    if failures:
        print(f"\n❌ 자동 항목 {len(failures)}건 실패 — cutover 불가")
        return 1
    print("\n✅ 자동 항목 전부 통과.")
    print("   ⚠️ 위 수동 항목까지 확인해야 cutover 조건을 만족한다. 자동 통과 = cutover 가능이 아니다.")
    return 0


_env, _rest = split_env_arg(sys.argv[1:])
raise SystemExit(asyncio.run(main(_env)))
