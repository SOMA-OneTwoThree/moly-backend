"""기존 사용자를 기억 파이프라인에 수동 등록한다.

활성 삭제 장벽과 현재 개인정보 버전을 확인하고, 사용자별 단일 트랜잭션에서
historical upper → collecting → earliest source turn → ready → 최초 잡 하나를 만든다.
기존 shadow/v2 사용자는 그대로 둔다. 기본은 롤백이며 실제 반영은 --yes.

    PYTHONPATH=. uv run python scripts/enter_shadow_cohort.py --env dev --limit 1
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import memory_pipeline
from db.envfile import announce, configure_application_db, split_env_arg
from db.maintenance import lock_enrollment_subject

# 후보 조회 뒤에도 쓰기 트랜잭션에서 삭제 장벽과 버전을 다시 검사한다.
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
    epoch = await lock_enrollment_subject(session, uid)
    upper = await memory_pipeline.enter_shadow(session, uid, privacy_epoch=epoch)
    if upper is None:
        return "이미 shadow/v2 — 건너뜀"

    earliest = await memory_pipeline.next_ingest_turn(session, uid, cursor=0)
    if earliest is None:
        return f"upper={upper} 이지만 처리할 source turn이 없다 — collecting 유지"

    if not await memory_pipeline.mark_bootstrap_ready(session, uid):
        return "collecting이 아니다 — ready 전환 안 함"

    await memory_pipeline.enqueue_ingest(session, uid, turn_seq=earliest, cursor=0, privacy_epoch=epoch)
    return f"upper={upper}, ready, 최초 잡 turn={earliest}"


async def main(env: str | None, users: list[str], limit: int, apply: bool) -> int:
    dsn = configure_application_db(env)
    announce(env, dsn, commit=apply)
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
                print(f"  ❌ {str(uid)[:8]}… {type(e).__name__}")

    print("\n" + "=" * 56)
    if not apply:
        print("dry-run이다 (전부 롤백). 실제 반영은 --yes 를 붙인다.")
    print(f"실패 {failed}명." if failed else "전부 성공.")
    return 1 if failed else 0


if __name__ == '__main__':
    _env, _rest = split_env_arg(sys.argv[1:])
    _p = argparse.ArgumentParser()
    _p.add_argument("--users", default="", help="쉼표로 구분한 user uuid")
    _p.add_argument("--limit", type=int, default=1)
    _p.add_argument("--yes", action="store_true")
    _a = _p.parse_args(_rest)
    if _a.limit < 1:
        _p.error("limit must be positive")
    raise SystemExit(asyncio.run(main(
        _env, [u for u in _a.users.split(",") if u], _a.limit, _a.yes,
    )))
