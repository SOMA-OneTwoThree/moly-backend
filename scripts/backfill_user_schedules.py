"""현재 user_schedules 누락·시간대 스냅샷을 수동 복구한다.

종류는 user_schedules.KINDS를 사용하고 기존 슬롯을 중복 생성하지 않는다.
NULL timezone의 기존 Asia/Seoul 대체 정책을 유지한다. 페이지별 커밋이므로
중간 실패 시 완료한 페이지는 남는다. 기본 롤백, 실제 반영 --yes.

    PYTHONPATH=. uv run python scripts/backfill_user_schedules.py --env dev --page 100
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import user_schedules
from db.envfile import announce, configure_application_db, split_env_arg

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
    dsn = configure_application_db(env)
    announce(env, dsn, commit=apply)
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
                    async with session.begin_nested():
                        changed = await user_schedules.ensure_for_user(
                            session, uid, timezone_name=tz, now=now
                        )
                        # 이미 있는 사용자라도 timezone이 바뀌었으면 다시 계산한다 — 안 하면
                        # 스냅샷이 옛 tz에 묶여 엉뚱한 시각에 알림이 간다(실측).
                        changed += await user_schedules.retime_for_user(
                            session, uid, timezone_name=tz, now=now
                        )
                    made += changed
                except Exception as e:  # noqa: BLE001  잘못된 IANA tz 등 — 배치를 멈추지 않는다
                    bad_tz.append(f"{str(uid)[:8]}… ({tz}): {type(e).__name__}")
            if apply:
                await session.commit()
            else:
                await session.rollback()
        print(f"  진행 {seen}명 (생성·재계산 {made}행)", flush=True)

    if bad_tz:
        print(f"\n⚠️ 복구 실패로 건너뛴 사용자 {len(bad_tz)}명:")
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
    if not ok or bad_tz:
        print("\n❌ 복구 또는 count gate 실패 — 실패한 사용자를 확인한다.")
        return 1
    print("\n✅ 현재 스케줄 count gate 통과.")
    return 0


if __name__ == '__main__':
    _env, _rest = split_env_arg(sys.argv[1:])
    _p = argparse.ArgumentParser()
    _p.add_argument("--yes", action="store_true")
    _p.add_argument("--page", type=int, default=500)
    _a = _p.parse_args(_rest)
    if _a.page < 1:
        _p.error("page must be positive")
    raise SystemExit(asyncio.run(main(_env, _a.yes, _a.page)))
