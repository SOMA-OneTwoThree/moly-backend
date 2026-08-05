"""관계 event·state backfill — 기존 사용자의 대화 이력에서 재생성.

기억 재설계 6단계(docs/capi-memory-ARCHITECTURE.md 15장 6번, 7.2절).

설계상 지키는 것:
 1. **런타임과 같은 계산 함수를 쓴다.** `app/services/relationship.py`가 유일한 판정 소스이며
    여기서 threshold를 다시 적으면 backfill과 런타임이 갈라진다.
 2. **관계 시작 시각은 `profiles`가 정본이다.** state에 복제하되 덮어쓰지 않는다.
 3. event가 있는데 첫 turn이 profile 시작 시각보다 **앞서면 그 사용자를 중단**시킨다 —
    좌표가 어긋난 채 state를 만들면 "처음 만난 날"이 틀어진다.
 4. **멱등**하다. dedup_key로 event가 중복되지 않고, 여러 번 돌려도 같은 state가 나온다.
 5. 기본 dry-run. `--commit`을 줘야 쓴다.

사용:
    PYTHONPATH=. uv run python scripts/backfill_relationship_state.py            # dry-run(dev)
    PYTHONPATH=. uv run python scripts/backfill_relationship_state.py --commit
    PYTHONPATH=. uv run python scripts/backfill_relationship_state.py --env prod --commit
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg

from app.services import relationship as rel
from db.envfile import announce, load_conn, split_env_arg

# keyset 페이지 크기 — 전 사용자를 한 번에 들지 않는다.
_PAGE = 200

_PROFILES = """
SELECT id, relationship_started_at
FROM profiles
WHERE id > $1
ORDER BY id
LIMIT $2
"""

# 성공 normal turn만. turn_seq가 없는 과거 행은 좌표가 없어 제외한다(추정하지 않는다).
_TURNS = """
SELECT DISTINCT turn_seq, activity_date, MIN(created_at) AS occurred_at
FROM messages
WHERE user_id = $1 AND kind = 'normal' AND turn_seq IS NOT NULL
GROUP BY turn_seq, activity_date
ORDER BY turn_seq
"""

_INSERT_EVENT = """
INSERT INTO relationship_events
  (user_id, event_type, turn_seq, activity_date, occurred_at, dedup_key)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (user_id, dedup_key) DO NOTHING
"""

_UPSERT_STATE = """
INSERT INTO user_relationship_states
  (user_id, relationship_started_at, active_days, successful_turns, qualifying_turns,
   last_interaction_at, relationship_stage, stage_rule_version, version, prompt_revision)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, 0)
ON CONFLICT (user_id) DO UPDATE SET
  relationship_started_at = EXCLUDED.relationship_started_at,
  active_days = EXCLUDED.active_days,
  successful_turns = EXCLUDED.successful_turns,
  qualifying_turns = EXCLUDED.qualifying_turns,
  last_interaction_at = EXCLUDED.last_interaction_at,
  -- stage는 단조 증가 — 재계산이 기존보다 낮아도 내리지 않는다(7.2절).
  relationship_stage = CASE
    WHEN array_position($9::text[], EXCLUDED.relationship_stage)
       > array_position($9::text[], user_relationship_states.relationship_stage)
    THEN EXCLUDED.relationship_stage
    ELSE user_relationship_states.relationship_stage END,
  stage_rule_version = EXCLUDED.stage_rule_version,
  version = user_relationship_states.version + 1,
  prompt_revision = CASE
    WHEN user_relationship_states.relationship_stage <> EXCLUDED.relationship_stage
      OR user_relationship_states.stage_rule_version <> EXCLUDED.stage_rule_version
    THEN user_relationship_states.prompt_revision + 1
    ELSE user_relationship_states.prompt_revision END,
  updated_at = now()
"""


async def _backfill_user(conn, user_id, started_at, *, commit: bool) -> dict:
    rows = await conn.fetch(_TURNS, user_id)
    if not rows:
        # 대화가 없는 profile도 정상적인 zero-event `new` state를 갖는다(7.2절).
        if commit:
            await conn.execute(
                _UPSERT_STATE, user_id, started_at, 0, 0, 0, None,
                rel.STAGE_NEW, rel.STAGE_RULE_VERSION, list(rel.STAGE_ORDER),
            )
        return {"turns": 0, "stage": rel.STAGE_NEW, "events": 0}

    first_occurred = min(r["occurred_at"] for r in rows)
    if started_at is not None and first_occurred < started_at:
        # 좌표가 어긋났다 — 추정으로 메우지 않고 이 사용자를 중단시킨다(불변식 3).
        return {
            "error": "first_turn_before_relationship_start",
            "first_turn": first_occurred,
            "started_at": started_at,
        }

    events = [(rel.EVENT_NORMAL_TURN, r["activity_date"], r["turn_seq"]) for r in rows]
    counters = rel.counters_from_events(events)
    stage = rel.compute_stage(counters.active_days, counters.qualifying_turns)
    last_at = max(r["occurred_at"] for r in rows)

    written = 0
    if commit:
        seen_days: set = set()
        for r in rows:
            await conn.execute(
                _INSERT_EVENT, user_id, rel.EVENT_NORMAL_TURN, r["turn_seq"],
                r["activity_date"], r["occurred_at"], rel.turn_dedup_key(r["turn_seq"]),
            )
            written += 1
            if r["activity_date"] not in seen_days:
                seen_days.add(r["activity_date"])
                await conn.execute(
                    _INSERT_EVENT, user_id, rel.EVENT_ACTIVE_DAY, r["turn_seq"],
                    r["activity_date"], r["occurred_at"],
                    rel.active_day_dedup_key(r["activity_date"]),
                )
                written += 1
        await conn.execute(
            _UPSERT_STATE, user_id, started_at, counters.active_days,
            counters.successful_turns, counters.qualifying_turns, last_at,
            stage, rel.STAGE_RULE_VERSION, list(rel.STAGE_ORDER),
        )
    return {
        "turns": counters.successful_turns,
        "days": counters.active_days,
        "qualifying": counters.qualifying_turns,
        "stage": stage,
        "events": written,
    }


async def main(env: str | None, commit: bool) -> int:
    dsn = load_conn(env)
    announce(env, dsn, commit=commit)
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    cursor = "00000000-0000-0000-0000-000000000000"
    total, blocked, by_stage = 0, [], {}
    try:
        while True:
            page = await conn.fetch(_PROFILES, cursor, _PAGE)
            if not page:
                break
            for p in page:
                out = await _backfill_user(
                    conn, p["id"], p["relationship_started_at"], commit=commit
                )
                total += 1
                if "error" in out:
                    blocked.append((p["id"], out))
                else:
                    by_stage[out["stage"]] = by_stage.get(out["stage"], 0) + 1
                cursor = str(p["id"])
    finally:
        await conn.close()

    print(f"\n대상 profile {total}명")
    for stage in rel.STAGE_ORDER:
        if by_stage.get(stage):
            print(f"  {stage:12s} {by_stage[stage]}명")
    if blocked:
        print(f"\n❌ 중단 {len(blocked)}명 — 좌표 불일치는 추정으로 메우지 않는다")
        for uid, out in blocked[:10]:
            print(f"  {uid}: 첫 turn {out['first_turn']} < 관계 시작 {out['started_at']}")
        return 1
    print("\n✅ 좌표 불일치 0명." + ("" if commit else " (dry-run — --commit으로 반영)"))
    return 0


_env, _rest = split_env_arg(sys.argv[1:])
raise SystemExit(asyncio.run(main(_env, "--commit" in _rest)))
