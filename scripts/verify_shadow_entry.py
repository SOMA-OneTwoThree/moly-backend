"""shadow 진입이 **실제로 backfill을 시작시키는지** 실 DB에서 확인한다 — 항상 롤백한다.

## 왜 스크립트인가

`tests/test_memory_pipeline.py`는 session을 흉내 내므로 SQL이 무엇을 하는지는 보지 못한다.
그래서 다음 결함이 테스트 1400개를 전부 통과한 채로 살아 있었다:

    `_ENTER_SHADOW`가 `source_through_turn_seq`를 올리지 않아 진입 직후 커서가 0이었다.
    `_NEXT_INGEST`는 `turn_seq <= source_through_turn_seq`를 요구하므로 아무 턴도 통과하지
    못했고, `enter_shadow_cohort.py`는 `mark_bootstrap_ready`에 닿기 전에 return했다.
    `collecting`은 live 잡도 안 받으므로(`accepts_live_ingest`) **영구 교착**이었다.

증상이 "에러"가 아니라 "조용히 아무 일도 안 일어남"이라, 실제로 돌려보지 않으면 드러나지 않는다.
운영 전환 전에 이 스크립트가 통과해야 한다.

## 무엇을 하는가

과거 대화가 있는 사용자를 골라 **트랜잭션 안에서** legacy로 되돌리고, 진짜 진입 경로를 태운 뒤
결과를 검사하고 롤백한다. DB는 변하지 않는다.

사용:
    PYTHONPATH=. uv run python scripts/verify_shadow_entry.py
"""
from __future__ import annotations

import asyncio
import sys
import uuid

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import memory_pipeline
from db.envfile import announce, load_conn, split_env_arg

# 과거 대화(turn_seq가 매겨진 normal 턴)가 가장 많은 사용자. 없으면 검사할 것이 없다.
_PICK = text("""
SELECT user_id, max(turn_seq) AS upper
FROM messages WHERE kind='normal' AND turn_seq IS NOT NULL
GROUP BY user_id ORDER BY 2 DESC LIMIT 1
""")

_TO_LEGACY = text("""
UPDATE memory_pipeline_states
SET mode='legacy', bootstrap_status='legacy', historical_upper_turn_seq=NULL,
    source_through_turn_seq=0, ingest_through_turn_seq=0, consolidated_through_turn_seq=0
WHERE user_id=:u
""")


async def run(session, uid: uuid.UUID, upper: int) -> list[str]:
    """진입 경로를 태우고 실패 사유를 모아 돌려준다. 빈 리스트 = 통과."""
    fail: list[str] = []
    await session.execute(_TO_LEGACY, {"u": uid})

    fixed = await memory_pipeline.enter_shadow(session, uid)
    if fixed != upper:
        fail.append(f"historical upper가 {fixed} — 실제 최대 턴 {upper}와 다르다")

    earliest = await memory_pipeline.next_ingest_turn(session, uid, cursor=0)
    if earliest is None:
        fail.append(
            f"진입 직후 다음 ingest 턴이 없다 — 과거 {upper}턴이 있는데도 backfill이 "
            "시작될 수 없다(source 커서가 0으로 남았다는 뜻)"
        )
    elif earliest != 1:
        fail.append(f"가장 이른 턴이 {earliest} — 1부터 시작해야 과거가 순서대로 흐른다")

    if not await memory_pipeline.mark_bootstrap_ready(session, uid):
        fail.append("collecting → ready 전환이 거부됐다 — 이 사용자는 영영 기억을 못 받는다")

    state = await memory_pipeline.load(session, uid)
    if not state.accepts_live_ingest:
        fail.append(f"ready인데 live ingest를 안 받는다(bootstrap_status={state.bootstrap_status})")
    return fail


async def main(env: str | None) -> int:
    announce(env, load_conn(env))
    async with get_sessionmaker()() as session:
        row = (await session.execute(_PICK)).first()
        if row is None:
            print("\n과거 턴을 가진 사용자가 없다 — 검사할 것이 없다.")
            return 0
        uid, upper = row[0], int(row[1])
        print(f"\n대상 {str(uid)[:8]}… (과거 {upper}턴)")
        try:
            fail = await run(session, uid, upper)
        finally:
            await session.rollback()
            print("롤백 완료 — DB 무변경")

    print()
    for f in fail:
        print(f"  ❌ {f}")
    if fail:
        print(f"\n{len(fail)}건 실패 — 이 상태로 운영 전환하면 해당 사용자들의 기억이 비어 있게 된다.")
        return 1
    print("  ✅ 진입 → 과거 턴 발견 → ready → live 수용까지 이어진다.")
    return 0


_env, _ = split_env_arg(sys.argv[1:])
raise SystemExit(asyncio.run(main(_env)))
