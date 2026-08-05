"""due dispatcher가 기존 전 profile 스캔과 **같은 사용자를 고르는지** 확인한다.

설계 15장 4번: 확인 전에 스캔을 제거하거나 scheduler read로 전환하지 않는다. 누락된
사용자는 전환 순간부터 일기도 알림도 못 받는데, 그건 조용히 일어난다.

두 경로를 같은 시각으로 돌려 대상 집합을 비교한다. **한 명이라도 다르면 전환하지 않는다.**

사용:
    PYTHONPATH=. uv run python scripts/verify_schedule_dispatcher.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg

from app.services import user_schedules as us
from db.envfile import announce, load_conn, split_env_arg


async def main(env: str | None) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    mismatches = 0
    try:
        profiles = await conn.fetch(
            "SELECT id, COALESCE(timezone,'Asia/Seoul') tz FROM profiles"
        )
        print(f"\n활성 profile {len(profiles)}명 · 24시간을 1시간 단위로 비교한다\n")

        base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for h in range(24):
            now = base + timedelta(hours=h)

            # ① 기존 경로: 전 profile을 훑어 로컬 시각이 목표시와 같은지 본다.
            scan: set[tuple[str, str]] = set()
            for p in profiles:
                try:
                    local_hour = now.astimezone(ZoneInfo(p["tz"])).hour
                except Exception:  # noqa: BLE001  잘못된 tz는 양쪽 다 건너뛴다
                    continue
                for kind, hour in us.LOCAL_HOUR.items():
                    if local_hour == hour:
                        scan.add((str(p["id"]), kind))

            # ② 새 경로: due 인덱스로 집는다.
            rows = await conn.fetch(
                """SELECT user_id, kind FROM user_schedules
                   WHERE next_due_at > $1 AND next_due_at <= $2""",
                now - timedelta(hours=1), now,
            )
            index = {(str(r["user_id"]), r["kind"]) for r in rows}

            only_scan = scan - index
            only_index = index - scan
            if only_scan or only_index:
                mismatches += 1
                print(f"  ❌ {now:%m-%d %H}시 UTC — 스캔만 {len(only_scan)} / 인덱스만 {len(only_index)}")
                for u, k in list(only_scan)[:3]:
                    print(f"       스캔에만: {u[:8]}… {k}")
                for u, k in list(only_index)[:3]:
                    print(f"       인덱스에만: {u[:8]}… {k}")
    finally:
        await conn.close()

    print("\n" + "=" * 56)
    if mismatches:
        print(f"❌ {mismatches}개 시각에서 대상이 다르다 — dispatcher를 켜면 안 된다.")
        print("   backfill이 최신인지, timezone_snapshot이 profile과 같은지 먼저 본다.")
        return 1
    print("✅ 24시간 전 구간에서 두 경로가 같은 대상을 고른다.")
    print("   ⚠️ 그래도 한 번 통과 = 전환 가능이 아니다. 시간을 두고 두 번 확인한다.")
    return 0


_env, _rest = split_env_arg(sys.argv[1:])
raise SystemExit(asyncio.run(main(_env)))
