"""schedule 4종 keyset backfill + count gate (15장 4번).

기존 profile 전부에 schedule 4종을 idempotent insert하고, **문서가 요구하는 게이트를 검사한다**:
활성 profile 수 `N`에 대해 종류별 count가 각각 `N`, 중복 0.

이 게이트를 통과하기 전에는 tick의 full-profile scan을 제거하거나 scheduler read로 전환하지
않는다 — 누락된 사용자는 그 순간부터 일기도 알림도 못 받는다.

keyset continuation을 쓴다(id > cursor). OFFSET은 뒤로 갈수록 느려지고 중간 삽입에 흔들린다.

기본은 dry-run이다. 실제 반영은 `--yes`.

사용:
    PYTHONPATH=. uv run python scripts/backfill_user_schedules.py
    PYTHONPATH=. uv run python scripts/backfill_user_schedules.py --yes
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import user_schedules
from db.envfile import announce, load_conn, split_env_arg

_PAGE = text("""
SELECT id, COALESCE(timezone, 'Asia/Seoul') AS tz
FROM profiles
WHERE id > :cursor
ORDER BY id
LIMIT :limit
""")

_GATE = text("""
SELECT
  (SELECT count(*) FROM profiles) AS profiles,
  (SELECT count(*) FROM user_schedules WHERE kind=:k) AS scheduled,
  (SELECT count(*) FROM (
     SELECT user_id FROM user_schedules WHERE kind=:k
     GROUP BY user_id HAVING count(*) > 1
   ) d) AS dupes
""")


async def main(env: str | None, apply: bool, page: int) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    maker = get_sessionmaker()
    now = datetime.now(timezone.utc)

    cursor = "00000000-0000-0000-0000-000000000000"
    seen = made = 0
    bad_tz: list[str] = []

    while True:
        async with maker() as session:
            rows = (await session.execute(_PAGE, {"cursor": cursor, "limit": page})).all()
            if not rows:
                break
            for uid, tz in rows:
                seen += 1
                cursor = str(uid)
                try:
                    made += await user_schedules.ensure_for_user(
                        session, uid, timezone_name=tz, now=now
                    )
                    # 이미 있는 사용자라도 timezone이 바뀌었으면 다시 계산한다 — 안 하면
                    # 스냅샷이 옛 tz에 묶여 엉뚱한 시각에 알림이 간다(실측).
                    made += await user_schedules.retime_for_user(
                        session, uid, timezone_name=tz, now=now
                    )
                except Exception as e:  # noqa: BLE001  잘못된 IANA tz 등 — 배치를 멈추지 않는다
                    bad_tz.append(f"{str(uid)[:8]}… ({tz}): {type(e).__name__}")
            if apply:
                await session.commit()
            else:
                await session.rollback()
        print(f"  진행 {seen}명 (생성 {made}행)", flush=True)

    if bad_tz:
        print(f"\n⚠️ timezone 문제로 건너뛴 사용자 {len(bad_tz)}명:")
        for b in bad_tz[:10]:
            print(f"  · {b}")

    print("\n" + "=" * 56)
    print("[count gate — 종류별 count = 활성 profile 수, 중복 0]")
    ok = True
    async with maker() as session:
        for kind in user_schedules.KINDS:
            profiles, scheduled, dupes = (await session.execute(_GATE, {"k": kind})).one()
            good = scheduled == profiles and dupes == 0
            ok &= good
            print(
                f"  {'✅' if good else '❌'} {kind:28s} {scheduled}/{profiles}"
                f"{'' if dupes == 0 else f'  중복 {dupes}'}"
            )

    if not apply:
        print("\ndry-run이라 게이트는 반영 전 상태를 본다. 실제 반영은 --yes 를 붙인다.")
        return 0
    if not ok:
        print("\n❌ count gate 실패 — scheduler read로 전환하면 안 된다.")
        return 1
    print("\n✅ count gate 통과. (그래도 tick의 full-profile scan은 아직 제거하지 않는다 —")
    print("   read 전환은 10단계 cutover의 일부다.)")
    return 0


_env, _rest = split_env_arg(sys.argv[1:])
_p = argparse.ArgumentParser()
_p.add_argument("--yes", action="store_true")
_p.add_argument("--page", type=int, default=500)
_a = _p.parse_args(_rest)
raise SystemExit(asyncio.run(main(_env, _a.yes, _a.page)))
