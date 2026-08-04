"""Unified semantic fact + verified episodic recall."""
from __future__ import annotations

import hashlib
import uuid
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.account import _uid
from app.services import privacy

_FACTS = text("""
WITH q AS (SELECT CAST(:embedding AS vector(1536)) embedding),
candidates AS (
  SELECT f.id,f.kind,f.canonical_text,f.event_time,f.importance,f.confidence,
         f.learned_at_watermark,1-(f.embedding <=> q.embedding) similarity
  FROM memory_facts f CROSS JOIN q
  WHERE f.user_id=:user_id AND f.status='active' AND f.embedding IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM memory_forget_markers m
      WHERE m.user_id=f.user_id AND (
        (m.scope='all' AND (m.future_learning='block' OR COALESCE(f.learned_at_watermark,0)<=m.cut_watermark))
        OR (m.scope='predicate' AND m.predicate=f.predicate
            AND (m.future_learning='block' OR COALESCE(f.learned_at_watermark,0)<=m.cut_watermark))
        OR (m.scope='fact' AND m.normalization_version=f.normalization_version
            AND m.normalized_hash=f.content_hash
            AND (m.future_learning='block' OR COALESCE(f.learned_at_watermark,0)<=m.cut_watermark))
      )
    )
  ORDER BY f.embedding <=> q.embedding
  LIMIT :candidate_limit
)
SELECT * FROM candidates
WHERE similarity>=:min_similarity
  AND (CAST(:from_date AS date) IS NULL OR event_time::date>=CAST(:from_date AS date))
  AND (CAST(:to_date AS date) IS NULL OR event_time::date<=CAST(:to_date AS date))
ORDER BY similarity DESC,importance DESC,confidence DESC,id
LIMIT :limit
""")

_EPISODES = text("""
WITH q AS (SELECT CAST(:embedding AS vector(1536)) embedding),
candidates AS (
  SELECT e.message_id,e.source_watermark,e.content_hash,m.content,m.created_at,
         1-(e.embedding <=> q.embedding) similarity
  FROM memory_episodic_messages e
  JOIN messages m ON m.user_id=e.user_id AND m.id=e.message_id AND m.sender='user'
  CROSS JOIN q
  WHERE e.user_id=:user_id AND e.embedding IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM memory_recall_suppressions s
      WHERE s.user_id=e.user_id AND s.message_id=e.message_id
    )
  ORDER BY e.embedding <=> q.embedding
  LIMIT :candidate_limit
)
SELECT * FROM candidates
WHERE similarity>=:min_similarity
  AND (CAST(:from_date AS date) IS NULL OR created_at::date>=CAST(:from_date AS date))
  AND (CAST(:to_date AS date) IS NULL OR created_at::date<=CAST(:to_date AS date))
ORDER BY similarity DESC,created_at DESC,message_id DESC
LIMIT :limit
""")


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


async def recall(
    session: AsyncSession,
    user_id: str | uuid.UUID,
    *,
    query: str,
    need: str = "summary",
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = 3,
    query_embedding: list[float],
) -> dict[str, Any]:
    uid = _uid(str(user_id)) if not isinstance(user_id, uuid.UUID) else user_id
    await privacy.ensure_subject_active(session, uid)
    if from_date and to_date and from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    maximum = max(1, min(limit, 5))
    params = {
        "user_id": uid,
        "embedding": _literal(query_embedding),
        "candidate_limit": max(20, maximum * 8),
        "min_similarity": settings.memory_search_min_similarity,
        "from_date": from_date,
        "to_date": to_date,
        "limit": maximum,
    }
    facts = (await session.execute(_FACTS, params)).mappings().all()
    episodes = (await session.execute(_EPISODES, params)).mappings().all()
    items: list[dict[str, Any]] = []
    for row in facts:
        items.append(
            {
                "ref": {"type": "memory_fact", "id": str(row["id"])},
                "id": str(row["id"]),
                "type": "fact",
                "kind": row["kind"],
                "text": row["canonical_text"],
                "observed_at": row["event_time"].isoformat() if row["event_time"] else None,
                "similarity": float(row["similarity"]),
            }
        )
    for row in episodes:
        content = str(row["content"] or "")
        # The projection never becomes quote authority. Recheck the original hash at read time.
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != row["content_hash"]:
            continue
        items.append(
            {
                "ref": {"type": "memory_episode", "id": str(row["message_id"])},
                "id": str(row["message_id"]),
                "type": "episode",
                "kind": "user_message",
                "text": content if need in {"quote", "exact"} else content[:400],
                "observed_at": row["created_at"].isoformat() if row["created_at"] else None,
                "similarity": float(row["similarity"]),
                "quote_verified": True,
            }
        )
    items.sort(key=lambda item: (item["similarity"], item["observed_at"] or ""), reverse=True)
    items = items[:maximum]
    return {
        "status": "ok",
        "matched_count": len(items),
        "returned_count": len(items),
        "coverage": "top_k",
        "has_more": False,
        "items": items,
    }
