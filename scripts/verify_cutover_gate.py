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
    (
        # 누락된 사용자는 scheduler 전환 순간부터 일기도 알림도 못 받는다(15장 4번).
        "schedule 4종이 빠진 profile",
        """SELECT count(*) FROM profiles p
           CROSS JOIN (VALUES ('daily_digest'),('diary_generate'),
                              ('diary_morning_notification'),('evening_checkin')) AS k(kind)
           WHERE NOT EXISTS (
             SELECT 1 FROM user_schedules s
             WHERE s.user_id = p.id AND s.kind = k.kind
           )""",
    ),
]

# ─────────────────────────────────────────────────────────────
# 존재 검사 — "있어야 할 게 있는가". 위 `_CHECKS`는 전부 "잘못된 게 없는가"라
# 데이터가 하나도 없으면 **전부 0으로 통과한다**(허위 양성). 실제로 provenance가 0건인
# 상태에서 "타 사용자를 가리키는 edge 0 / assistant 근거 edge 0"이 통과했고, 그걸
# "안전하다"고 보고했다. 없는 것을 안전으로 읽지 않도록 여기서 하한을 본다.
#
# (이름, SQL, 최소 기대값)
_PRESENCE: list[tuple[str, str, int]] = [
    (
        "기억 provenance(source edge)",
        """SELECT count(*) FROM mem0_memory_sources s
           JOIN mem0_memory_registry r ON r.id = s.registry_id
           WHERE r.semantic_status IN ('active','ambiguous')""",
        1,
    ),
    (
        "추출 candidate의 근거 edge",
        "SELECT count(*) FROM mem0_ingest_candidate_sources",
        1,
    ),
    (
        "published interaction contract",
        "SELECT count(*) FROM user_interaction_contracts WHERE status='published'",
        1,
    ),
    (
        "relationship render",
        "SELECT count(*) FROM relationship_profile_renders",
        1,
    ),
    (
        "v2 사용자의 검색 가능한 기억",
        """SELECT count(*) FROM mem0_memory_registry r
           JOIN memory_pipeline_states s ON s.user_id = r.user_id
           WHERE s.mode = 'v2' AND r.semantic_status IN ('active','ambiguous')""",
        1,
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
    "golden recall 기준 통과 (case 200건 작성 필요 — 제품 판단)",
    "1x/2x/5x 부하에서 목표 처리량 (memory 큐 분리 후 재측정 필요)",
    "worker kill·DB delay 주입 뒤 정합성",
    "100건 초과 삭제와 두 sweep 0",
    # 2026-08-05 dev 실측 통과: 동시 8회 claim에서 승자 1건, 재청구 0건.
    # 창(expiry)은 tests/test_notification_expiry.py가 CI에서 지킨다.
    "알림 dedup 재실행 (scripts/verify_notification_fixture.py — claim 로직 변경 시마다)",
]


async def main(env: str | None) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    failures: list[tuple[str, int]] = []
    try:
        print("\n[자동 판정 — 잘못된 게 없는가]")
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
        print("\n[존재 판정 — 있어야 할 게 있는가]")
        for name, sql, minimum in _PRESENCE:
            try:
                got = int(await conn.fetchval(sql) or 0)
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠️  {name}: 조회 실패 — {type(e).__name__}")
                failures.append((name, -1))
                continue
            ok = got >= minimum
            print(f"  {'✅' if ok else '❌'} {name}: {got} (최소 {minimum})")
            if not ok:
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
