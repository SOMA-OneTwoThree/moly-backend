"""registry 전이 저장소 — 판정 결과를 원자적으로 반영한다.

구조·불변 규칙: docs/ARCHITECTURE.md 「장기 기억과 사용자별 상태」.

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

# Foreground publication locks chat_contexts before supplied memory rows. Take
# that same lock before a destructive transition; waiting on it after the
# registry UPDATE would invert publication's order. No advisory lock is added:
# worker finalization already owns its job row, which foreground may enqueue.
_LOCK_COMPAT_SUMMARY = text("""
SELECT user_id FROM chat_contexts
WHERE user_id=:user_id AND retention_compat_json IS NOT NULL
  AND retention_compat_json->>'user_id'=user_id::text
  AND retention_compat_json->'generation'=to_jsonb(memory_generation)
FOR UPDATE
""")

_INVALIDATE_COMPAT_SUMMARY = text("""
UPDATE chat_contexts SET retention_compat_summary_invalidated_revision=COALESCE(
  retention_compat_summary_invalidated_revision,GREATEST(context_revision,0)+1)
WHERE user_id=:user_id AND retention_compat_json IS NOT NULL
  AND retention_compat_json->>'user_id'=user_id::text
  AND retention_compat_json->'generation'=to_jsonb(memory_generation)
RETURNING user_id
""")

_DESTRUCTIVE_STATUSES = frozenset({"superseded", "excluded"})

_PROPAGATE_REQUIRED = text("""
UPDATE mem0_memory_registry winner
SET context_priority='required',
    canonical_text=CASE WHEN winner.canonical_text IS NULL
      AND old.duplicate_of_registry_id=winner.id AND old.content_hash=winner.content_hash
      THEN old.canonical_text ELSE winner.canonical_text END,
    revision=winner.revision+1,updated_at=now()
FROM mem0_memory_registry old
WHERE winner.user_id=:user_id AND old.user_id=winner.user_id
  AND old.context_priority='required'
  AND old.id=ANY(CAST(:ids AS uuid[]))
  AND winner.semantic_status='active'
  AND ((old.semantic_status='duplicate' AND old.duplicate_of_registry_id=winner.id)
    OR (old.semantic_status='superseded' AND old.superseded_by_registry_id=winner.id))
""")

_COPY_DUPLICATE_SOURCES = text("""
INSERT INTO mem0_memory_sources
 (registry_id,user_id,source_turn_seq,source_message_id,source_sender,evidence_start_utf8,
  evidence_end_utf8,source_content_hash,source_occurred_at,source_activity_date,
  authority,confidence,extractor_version)
SELECT winner.id,s.user_id,s.source_turn_seq,s.source_message_id,s.source_sender,
       s.evidence_start_utf8,s.evidence_end_utf8,s.source_content_hash,s.source_occurred_at,
       s.source_activity_date,s.authority,s.confidence,s.extractor_version
FROM mem0_memory_registry old
JOIN mem0_memory_registry winner ON winner.id=old.duplicate_of_registry_id AND winner.user_id=old.user_id
JOIN mem0_memory_sources s ON s.registry_id=old.id AND s.user_id=old.user_id
WHERE old.user_id=:user_id AND old.id=ANY(CAST(:ids AS uuid[]))
  AND old.semantic_status='duplicate' AND old.context_priority='required'
  AND winner.semantic_status='active' AND winner.content_hash=old.content_hash
ON CONFLICT DO NOTHING
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
    compat_locked = False
    if any(t.semantic_status in _DESTRUCTIVE_STATUSES for t in transitions):
        compat_locked = (await session.execute(
            _LOCK_COMPAT_SUMMARY, {"user_id": user_id}
        )).first() is not None
    compat_invalidated = False
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
            if (compat_locked and not compat_invalidated
                    and t.semantic_status in _DESTRUCTIVE_STATUSES):
                # Record only an actually applied semantic retirement. A
                # terminal/no-op or a duplicate compaction must not quarantine
                # otherwise usable historical context. The context lock is
                # already held, so this cannot add registry -> context waiting.
                invalidated = (await session.execute(
                    _INVALIDATE_COMPAT_SUMMARY, {"user_id": user_id}
                )).first()
                if invalidated is None:
                    raise StaleConsolidation("compatibility binding changed")
                compat_invalidated = True
    linked = [transition.registry_id for transition in transitions
              if transition.duplicate_of is not None or transition.superseded_by is not None]
    if linked:
        await session.execute(_PROPAGATE_REQUIRED, {"user_id": user_id, "ids": linked})
        await session.execute(_COPY_DUPLICATE_SOURCES, {"user_id": user_id, "ids": linked})
    return changed
