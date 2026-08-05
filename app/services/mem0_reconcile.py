"""mem0 고아 정리 — crash로 남은 planned 후보와 미등록 벡터를 수렴시킨다.

기억 재설계(docs/capi-memory-ARCHITECTURE.md 9.4절 crash recovery).

crash 지점별로 남는 흔적이 다르다.
 · vector upsert **전** crash → planned 후보만 남는다. provider에는 없다
 · vector upsert **후**, registry 저장 전 crash → planned 후보 + provider 벡터가 남고
   registry가 없다. **registry에 없는 provider 결과는 검색에서 쓰지 않으므로** 노출되진
   않지만 영원히 떠 있는다

**provider collection을 무제한 scan해 추측하지 않는다.** durable planned 후보의 결정 UUID로만
확인한다 — 그게 우리가 만든 것의 유일한 신뢰 가능한 목록이다.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("moly-worker")

# 이 시간보다 오래 planned로 남아 있으면 고아로 본다. 진행 중인 잡을 건드리지 않을 만큼 넉넉히.
ORPHAN_AFTER_MINUTES = 30

_ORPHANS = text("""
SELECT c.id, c.user_id, c.provider_memory_id, c.turn_seq
FROM mem0_ingest_candidates c
WHERE c.status = 'planned'
  AND c.created_at < now() - make_interval(mins => :minutes)
  -- registry가 이미 있으면 고아가 아니다(닫기만 실패한 것).
  AND NOT EXISTS (
    SELECT 1 FROM mem0_memory_registry r
    WHERE r.user_id = c.user_id AND r.provider_memory_id = c.provider_memory_id
  )
ORDER BY c.created_at
LIMIT :limit
""")

# registry는 있는데 후보가 planned로 남은 경우 — 닫기만 실패했다.
_CLOSE_STRAGGLERS = text("""
UPDATE mem0_ingest_candidates c
SET status='committed', updated_at=now()
WHERE c.status='planned'
  AND EXISTS (
    SELECT 1 FROM mem0_memory_registry r
    WHERE r.user_id = c.user_id AND r.provider_memory_id = c.provider_memory_id
  )
RETURNING c.id
""")

_MARK_DEAD = text("""
UPDATE mem0_ingest_candidates SET status='dead', updated_at=now()
WHERE id = :id AND status = 'planned'
""")


@dataclass(frozen=True, slots=True)
class OrphanCandidate:
    candidate_id: uuid.UUID
    user_id: uuid.UUID
    provider_memory_id: uuid.UUID
    turn_seq: int


async def close_stragglers(session: AsyncSession) -> int:
    """registry가 이미 있는데 planned로 남은 후보를 닫는다. 가장 흔한 잔재다."""
    rows = (await session.execute(_CLOSE_STRAGGLERS)).all()
    return len(rows)


async def find_orphans(session: AsyncSession, *, limit: int = 100) -> list[OrphanCandidate]:
    """registry 없이 오래 planned로 남은 후보. bounded."""
    rows = (
        await session.execute(_ORPHANS, {"minutes": ORPHAN_AFTER_MINUTES, "limit": limit})
    ).all()
    return [
        OrphanCandidate(
            candidate_id=r[0], user_id=r[1], provider_memory_id=r[2], turn_seq=r[3]
        )
        for r in rows
    ]


async def resolve_orphan(
    session: AsyncSession, orphan: OrphanCandidate, *, exists_in_provider: bool
) -> str:
    """고아 하나를 정리한다. 반환 = 취한 조치.

    provider에 벡터가 남아 있으면 **삭제**한다 — registry가 없으면 검색에 안 쓰이므로
    그대로 두면 영원히 떠 있는 쓰레기다. 없으면 후보만 dead로 닫는다.
    """
    await session.execute(_MARK_DEAD, {"id": orphan.candidate_id})
    return "deleted_vector" if exists_in_provider else "closed_only"
