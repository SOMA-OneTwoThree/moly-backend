"""삭제 장벽 backfill 검증 — 15장 2단계 (d)의 게이트.

`privacy_barrier_mode=enforced`로 올리기 전에 **연속 두 번** 통과해야 한다. 한 번만 보고 올리면
backfill이 끝나지 않은 상태에서 fail-closed가 걸려 전 사용자가 막힌다.

사용:
    PYTHONPATH=. uv run python scripts/verify_privacy_barriers.py            # dev
    PYTHONPATH=. uv run python scripts/verify_privacy_barriers.py --env prod
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg

from db.envfile import announce, load_conn, split_env_arg

_CHECKS = {
    "profiles": "SELECT count(*) FROM public.profiles",
    "barriers": "SELECT count(*) FROM public.privacy_subject_barriers",
    "active": "SELECT count(*) FROM public.privacy_subject_barriers WHERE state='active'",
    "deleting": "SELECT count(*) FROM public.privacy_subject_barriers WHERE state='deleting'",
    "deleted": "SELECT count(*) FROM public.privacy_subject_barriers WHERE state='deleted'",
    # 장벽 없는 profile — enforced로 올리면 이 사용자들이 전부 막힌다. 반드시 0이어야 한다.
    "missing": """
        SELECT count(*) FROM public.profiles p
        WHERE NOT EXISTS (
          SELECT 1 FROM public.privacy_subject_barriers b WHERE b.user_id = p.id
        )""",
    # 고아 장벽(profile 없는 행) — deleted tombstone은 정상이므로 그 외만 센다.
    "orphan_non_deleted": """
        SELECT count(*) FROM public.privacy_subject_barriers b
        WHERE b.state <> 'deleted'
          AND NOT EXISTS (SELECT 1 FROM public.profiles p WHERE p.id = b.user_id)""",
    # active인데 operation_id가 있는 행 = 상태 오염.
    "active_with_operation": """
        SELECT count(*) FROM public.privacy_subject_barriers
        WHERE state='active' AND operation_id IS NOT NULL""",
}


async def main(env: str | None) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        got = {name: await conn.fetchval(sql) for name, sql in _CHECKS.items()}
    finally:
        await conn.close()

    for name, value in got.items():
        print(f"  {name:22s} {value}")

    problems = []
    if got["missing"] != 0:
        problems.append(f"장벽 없는 profile {got['missing']}건 — enforced로 올리면 전부 차단된다")
    if got["orphan_non_deleted"] != 0:
        problems.append(f"고아 장벽 {got['orphan_non_deleted']}건(deleted tombstone 제외)")
    if got["active_with_operation"] != 0:
        problems.append(f"operation_id가 붙은 active 행 {got['active_with_operation']}건")
    expected = got["active"] + got["deleting"] + got["deleted"]
    if expected != got["barriers"]:
        problems.append(f"state 합계 {expected} != 전체 {got['barriers']} (알 수 없는 state)")

    if problems:
        print("\n❌ 게이트 실패 — enforced로 올리지 말 것")
        for p in problems:
            print(f"  · {p}")
        return 1
    print("\n✅ 게이트 통과. 연속 두 번 통과했을 때만 privacy_barrier_mode=enforced로 올린다.")
    return 0


_env, _rest = split_env_arg(sys.argv[1:])
raise SystemExit(asyncio.run(main(_env)))
