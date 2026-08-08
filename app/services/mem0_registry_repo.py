"""registry 전이 저장소 — 판정 결과를 원자적으로 반영한다.

기억 재설계(docs/ARCHITECTURE-capi.md 5.3절·5.4절).

**component 전체를 한 transaction에서 publish한다.** 일부만 반영하면 supersede된 기억은 닫혔는데
새 기억은 active가 아닌 상태가 생겨, 그 사이 검색에 아무것도 안 잡힌다.

외부 호출 구간의 advisory lock은 쓰지 않는다. 사용자별 pipeline state의 revision을 CAS로
확인하는 짧은 transaction만 쓴다.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.mem0_consolidation import Transition

# 이 turn에서 만들어진, 아직 판정 안 된 기억.
_PENDING = text("""
SELECT r.id, r.provider_memory_id, r.source_turn_seq, r.content_hash,
       COALESCE(s.source_occurred_at, r.created_at) AS occurred_at
FROM mem0_memory_registry r
LEFT JOIN LATERAL (
  SELECT MAX(source_occurred_at) AS source_occurred_at
  FROM mem0_memory_sources ms WHERE ms.registry_id = r.id
) s ON true
WHERE r.user_id = :user_id AND r.source_turn_seq = :turn_seq
  AND r.semantic_status = 'pending'
ORDER BY r.created_at
""")

# 비교 대상 — 같은 사용자의 살아 있는 기억만. 다른 사용자는 SQL에서 배제한다.
#
# ⚠️ 고르는 기준은 **턴 번호가 최근인 것**이지 의미가 비슷한 것이 아니다. 오래전에 한 얘기를
#    다시 하면 여기서는 중복으로 안 잡힌다. 그건 하루 경계 재판정이 맡는다.
# ⚠️ 동점 기준(`id`)이 없으면 같은 turn_seq가 24건씩 있을 때 매번 다른 것이 뽑혀 결과가
#    비결정이 된다.
_COMPARISON_POOL = text("""
SELECT r.id, r.provider_memory_id, r.source_turn_seq, r.content_hash,
       COALESCE(s.source_occurred_at, r.created_at) AS occurred_at
FROM mem0_memory_registry r
LEFT JOIN LATERAL (
  SELECT MAX(source_occurred_at) AS source_occurred_at
  FROM mem0_memory_sources ms WHERE ms.registry_id = r.id
) s ON true
WHERE r.user_id = :user_id
  AND r.semantic_status IN ('active','ambiguous')
  AND r.source_turn_seq < :turn_seq
ORDER BY r.source_turn_seq DESC, r.id DESC
LIMIT :limit
""")

# 전이 — 판정 시점의 revision을 확인해 stale 결과를 막는다.
_APPLY = text("""
UPDATE mem0_memory_registry
SET semantic_status = :semantic_status,
    provider_delete_state = :provider_delete_state,
    duplicate_of_registry_id = :duplicate_of,
    superseded_by_registry_id = :superseded_by,
    conflict_group_id = :conflict_group_id,
    classification_version = :classification_version,
    revision = revision + 1,
    updated_at = now()
WHERE id = :id AND user_id = :user_id
  -- pending에서만 전진하거나, 기존 active를 닫는 경우만 허용한다.
  AND semantic_status IN ('pending','active','ambiguous')
RETURNING id
""")

# 같은 사용자의 pipeline revision이 바뀌었으면 판정이 낡았다.
_CHECK_REVISION = text("""
SELECT revision, privacy_epoch FROM memory_pipeline_states WHERE user_id = :user_id
""")


class StaleConsolidation(RuntimeError):
    """판정 중 사용자 상태가 바뀌었다 — publish하지 않고 다시 판정한다."""


async def load_pending(session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int) -> list[dict]:
    rows = (await session.execute(_PENDING, {"user_id": user_id, "turn_seq": turn_seq})).all()
    return [
        {"id": r[0], "provider_memory_id": r[1], "source_turn_seq": r[2],
         "content_hash": r[3], "occurred_at": r[4]}
        for r in rows
    ]


async def load_comparison_pool(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int, limit: int = 12
) -> list[dict]:
    rows = (
        await session.execute(
            _COMPARISON_POOL, {"user_id": user_id, "turn_seq": turn_seq, "limit": limit}
        )
    ).all()
    return [
        {"id": r[0], "provider_memory_id": r[1], "source_turn_seq": r[2],
         "content_hash": r[3], "occurred_at": r[4]}
        for r in rows
    ]


async def apply_transitions(
    session: AsyncSession,
    user_id: uuid.UUID,
    transitions: list[Transition],
    *,
    expected_revision: int,
    classification_version: str,
) -> int:
    """component 전체를 한 transaction에서 반영한다. 반환 = 실제 변경된 행 수.

    커밋은 호출측 소유 — 커서 전진과 같은 transaction에 묶을 수 있다.
    """
    row = (await session.execute(_CHECK_REVISION, {"user_id": user_id})).first()
    if row is None or int(row[0]) != expected_revision:
        raise StaleConsolidation(
            f"pipeline revision이 바뀌었다: 기대 {expected_revision} / 실제 {row[0] if row else None}"
        )
    changed = 0
    for t in transitions:
        got = (
            await session.execute(
                _APPLY,
                {
                    "id": t.registry_id,
                    "user_id": user_id,
                    "semantic_status": t.semantic_status,
                    "provider_delete_state": t.provider_delete_state,
                    "duplicate_of": t.duplicate_of,
                    "superseded_by": t.superseded_by,
                    "conflict_group_id": t.conflict_group_id,
                    "classification_version": classification_version,
                },
            )
        ).first()
        if got is not None:
            changed += 1
    return changed
