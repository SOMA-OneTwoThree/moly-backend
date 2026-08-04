from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.schemas.memory import MemoryForgetRequest, MemorySearchRequest
from app.services import memory_embeddings, memory_forget, memory_registry, memory_repo, privacy

_LIST_SQL = text("""
SELECT f.id, f.kind, f.canonical_text, f.predicate, f.event_time
FROM memory_facts f
WHERE f.user_id=:user_id AND f.status='active'
  AND NOT EXISTS (
    SELECT 1 FROM memory_forget_markers m WHERE m.user_id=f.user_id
      AND (m.future_learning='block' OR COALESCE(f.learned_at_watermark,0)<=m.cut_watermark)
      AND (
      m.scope='all'
      OR (m.scope='predicate' AND f.predicate IS NOT NULL AND m.predicate=f.predicate)
      OR (m.scope='fact' AND m.normalization_version=f.normalization_version
                         AND m.normalized_hash=f.content_hash)
    )
  )
ORDER BY f.importance DESC, f.updated_at DESC, f.id
LIMIT 100
""")


async def list_facts(session: AsyncSession, user_id: str) -> dict:
    uid = uuid.UUID(user_id)
    await privacy.ensure_subject_active(session, uid)
    rows = (await session.execute(_LIST_SQL, {"user_id": uid})).mappings().all()
    return {
        "items": [
            {
                "id": row["id"],
                "kind": row["kind"],
                "text": row["canonical_text"],
                "predicate": row["predicate"],
                "event_time": row["event_time"],
            }
            for row in rows
        ]
    }


async def search(session: AsyncSession, user_id: str, req: MemorySearchRequest) -> dict:
    await privacy.ensure_subject_active(session, uuid.UUID(user_id))
    embedding = await memory_embeddings.embed_query(req.query)
    rows = await memory_repo.search_memory(
        session,
        uuid.UUID(user_id),
        embedding=embedding,
        from_date=req.from_date,
        to_date=req.to_date,
        limit=20,
        min_similarity=settings.memory_search_min_similarity,
    )
    return {
        "items": [
            {
                "id": row.id,
                "kind": row.kind,
                "text": row.text,
                "observed_at": row.observed_at,
                "similarity": row.similarity,
            }
            for row in rows
        ]
    }


async def forget(session: AsyncSession, user_id: str, req: MemoryForgetRequest) -> dict:
    await privacy.ensure_subject_active(session, uuid.UUID(user_id))
    if req.scope == "predicate" and not memory_registry.is_predicate(req.predicate):
        raise errors.validation("지원하지 않는 기억 범위입니다.")
    request = memory_forget.ForgetRequest(
        scope=req.scope,
        fact_ids=tuple(req.fact_ids),
        predicate=req.predicate,
        future_learning=req.future_learning,
    )
    result = await memory_forget.apply(
        session, user_id=uuid.UUID(user_id), request=request
    )
    await session.commit()
    return {
        "status": result.status,
        "forgotten_fact_ids": list(result.forgotten_facts),
        "invalidated_insight_ids": list(result.invalidated_insights),
        "memory_generation": result.memory_generation,
    }
