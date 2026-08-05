"""알림 dedup 실측 — 동시 claim에서 정확히 하나만 이긴다.

09:00·20:00 푸시는 유저×활동일당 1회다. 그 보장은 `user_daily_stats`의 조건부 upsert
(`where=col.is_(None)`)에 전적으로 의존한다. 단위 테스트는 이 함수를 mock하므로
**실제로 원자적인지는 아무도 확인하지 않았다.**

15분 케이던스 워커가 겹쳐 뜨거나 두 EC2가 같은 유저를 동시에 집으면 여기가 유일한 방어선이다.
그래서 **같은 유저·같은 활동일에 동시 claim을 걸어** 승자가 하나인지 실측한다.

⚠️ 실 DB에 임시 행을 쓰고 지운다. provider 호출·과금은 없다.

사용:
    PYTHONPATH=. uv run python scripts/verify_notification_fixture.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone

import asyncpg

from db.envfile import announce, load_conn, split_env_arg

_CONCURRENCY = 8

# notify._claim_send_slot 과 **같은 조건부 upsert**. 여기가 갈라지면 이 fixture는 무의미하다.
_CLAIM = """
INSERT INTO user_daily_stats (user_id, activity_date, {col})
VALUES ($1, $2, now())
ON CONFLICT (user_id, activity_date) DO UPDATE SET {col} = now()
WHERE user_daily_stats.{col} IS NULL
RETURNING id
"""


async def _claim(pool, uid, day, col) -> bool:
    async with pool.acquire() as c:
        return await c.fetchval(_CLAIM.format(col=col), uid, day) is not None


async def _run_case(pool, uid, day, col, label) -> bool:
    """동시에 여러 번 claim. 정확히 1건만 True여야 한다."""
    wins = sum(await asyncio.gather(*(
        _claim(pool, uid, day, col) for _ in range(_CONCURRENCY)
    )))
    ok = wins == 1
    print(f"  {'✅' if ok else '❌'} {label}: 동시 {_CONCURRENCY}회 중 승자 {wins}건 (기대 1)")
    return ok


async def main(env: str | None) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    pool = await asyncpg.create_pool(
        dsn.replace("postgresql+asyncpg://", "postgresql://"),
        min_size=_CONCURRENCY, max_size=_CONCURRENCY,
    )
    uid = None
    ok = True
    try:
        async with pool.acquire() as c:
            uid = await c.fetchval("SELECT id FROM profiles LIMIT 1")
        if uid is None:
            print("\n❌ profiles가 비었다 — 검사할 유저가 없다.")
            return 1

        today = datetime.now(timezone.utc).date()
        d1, d2 = date(today.year, 1, 1), date(today.year, 1, 2)
        async with pool.acquire() as c:
            await c.execute(
                "DELETE FROM user_daily_stats WHERE user_id=$1 AND activity_date = ANY($2::date[])",
                uid, [d1, d2],
            )

        print(f"\n[동시 claim] 유저 {str(uid)[:8]}…  동시성 {_CONCURRENCY}")
        ok &= await _run_case(pool, uid, d1, "morning_notified_at", "아침 09:00")
        ok &= await _run_case(pool, uid, d1, "evening_notified_at", "저녁 20:00")

        print("\n[독립성] 다른 활동일은 서로를 막지 않는다")
        ok &= await _run_case(pool, uid, d2, "morning_notified_at", "다음 활동일 아침")

        print("\n[재청구] 이미 발송한 슬롯은 다시 이길 수 없다")
        again = sum(await asyncio.gather(*(
            _claim(pool, uid, d1, "morning_notified_at") for _ in range(_CONCURRENCY)
        )))
        good = again == 0
        ok &= good
        print(f"  {'✅' if good else '❌'} 재청구 승자 {again}건 (기대 0)")

        async with pool.acquire() as c:
            await c.execute(
                "DELETE FROM user_daily_stats WHERE user_id=$1 AND activity_date = ANY($2::date[])",
                uid, [d1, d2],
            )
    finally:
        await pool.close()

    print("\n" + "=" * 52)
    print("✅ 알림 dedup fixture 통과" if ok else "❌ 알림 dedup fixture 실패")
    return 0 if ok else 1


_env, _rest = split_env_arg(sys.argv[1:])
raise SystemExit(asyncio.run(main(_env)))
