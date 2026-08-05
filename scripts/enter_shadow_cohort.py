"""shadow 진입 오케스트레이터 — 15장 5·7단계의 운영 경로.

`enter_shadow()`와 `mark_bootstrap_ready()`는 도메인 함수일 뿐 **부르는 곳이 없었다.**
그래서 어떤 사용자도 코드 경로로는 shadow에 들어가지 못했다(SQL 수기 투입만 가능했다).
이 스크립트가 그 경로다.

사용자 하나를 **단일 transaction**으로 처리하며 문서가 요구하는 순서를 코드로 강제한다:

  1. shadow 진입 — historical upper를 고정하고 `bootstrap_status=collecting`
  2. **tombstone 이관이 끝났는지 먼저 확인한다.** 안 끝났으면 그 사용자는 통째로 롤백한다 —
     순서가 뒤집히면 지워달라던 내용이 v2 기억으로 되살아난다(15장 7번, cutover 즉시 중단 사유)
  3. tombstone 제외 후 가장 이른 source turn을 구한다
  4. `bootstrap_status=ready`로 바꾸고 **정확히 그 turn 하나만** enqueue한다.
     이후는 성공 finalize가 `MIN(turn_seq)>cursor`로 이어받는다

기본은 dry-run이다. 실제 반영은 `--yes`.

사용:
    PYTHONPATH=. uv run python scripts/enter_shadow_cohort.py --limit 1
    PYTHONPATH=. uv run python scripts/enter_shadow_cohort.py --users <uuid> --yes
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import memory_pipeline
from db.envfile import announce, load_conn, split_env_arg

# tombstone 이관이 남았는지. legacy 마커가 있는데 tombstone이 하나도 없으면 미이관이다.
_TOMBSTONE_READY = text("""
SELECT
  (SELECT count(*) FROM memory_recall_suppressions WHERE user_id=:u)
  + (SELECT count(*) FROM memory_source_closures WHERE user_id=:u)
  + (SELECT count(*) FROM memory_forget_markers WHERE user_id=:u) AS legacy,
  (SELECT count(*) FROM legacy_recall_tombstones WHERE user_id=:u) AS migrated
""")

_CANDIDATES = text("""
SELECT p.id
FROM profiles p
LEFT JOIN memory_pipeline_states s ON s.user_id = p.id
WHERE COALESCE(s.mode, 'legacy') = 'legacy'
  AND EXISTS (
    SELECT 1 FROM messages m
    WHERE m.user_id = p.id AND m.kind='normal' AND m.turn_seq IS NOT NULL
  )
ORDER BY p.id
LIMIT :limit
""")


async def _one(session, uid: uuid.UUID, *, apply: bool) -> str:
    upper = await memory_pipeline.enter_shadow(session, uid)
    if upper is None:
        return "이미 shadow/v2 — 건너뜀"

    legacy, migrated = (await session.execute(_TOMBSTONE_READY, {"u": uid})).one()
    if legacy and not migrated:
        # 여기서 멈추지 않으면 지워달라던 내용이 v2로 되살아난다.
        raise RuntimeError(
            f"tombstone 미이관 (legacy {legacy}건, tombstone 0건) — "
            "migrate_legacy_tombstones.py 를 먼저 돌려라"
        )

    earliest = await memory_pipeline.next_ingest_turn(session, uid, cursor=0)
    if earliest is None:
        return f"upper={upper} 이지만 처리할 source turn이 없다 — collecting 유지"

    if not await memory_pipeline.mark_bootstrap_ready(session, uid):
        return "collecting이 아니다 — ready 전환 안 함"

    await memory_pipeline.enqueue_ingest(session, uid, turn_seq=earliest)
    return f"upper={upper}, ready, 최초 잡 turn={earliest} (tombstone {migrated}건)"


async def main(env: str | None, users: list[str], limit: int, apply: bool) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    maker = get_sessionmaker()

    async with maker() as session:
        if users:
            targets = [uuid.UUID(u) for u in users]
        else:
            targets = [r[0] for r in (await session.execute(_CANDIDATES, {"limit": limit})).all()]

    if not targets:
        print("\n대상 사용자가 없다 (legacy 상태이면서 normal turn이 있는 사용자).")
        return 0

    print(f"\n대상 {len(targets)}명 — {'반영' if apply else 'dry-run'}")
    failed = 0
    for uid in targets:
        # 사용자마다 독립 transaction. 한 명이 실패해도 나머지는 진행한다.
        async with maker() as session:
            try:
                msg = await _one(session, uid, apply=apply)
                if apply:
                    await session.commit()
                else:
                    await session.rollback()
                print(f"  ✅ {str(uid)[:8]}… {msg}")
            except Exception as e:  # noqa: BLE001
                await session.rollback()
                failed += 1
                print(f"  ❌ {str(uid)[:8]}… {e}")

    print("\n" + "=" * 56)
    if not apply:
        print("dry-run이다 (전부 롤백). 실제 반영은 --yes 를 붙인다.")
    print(f"실패 {failed}명." if failed else "전부 성공.")
    return 1 if failed else 0


_env, _rest = split_env_arg(sys.argv[1:])
_p = argparse.ArgumentParser()
_p.add_argument("--users", default="", help="쉼표로 구분한 user uuid")
_p.add_argument("--limit", type=int, default=1)
_p.add_argument("--yes", action="store_true")
_a = _p.parse_args(_rest)
raise SystemExit(asyncio.run(main(
    _env, [u for u in _a.users.split(",") if u], _a.limit, _a.yes,
)))
