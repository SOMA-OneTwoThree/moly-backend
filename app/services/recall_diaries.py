"""Answer-complete diary recall used by the agent and Dev diagnostics."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile
from app.services.account import _uid
from app.services import naming, privacy

MAX_RETURNED = 5

_RECALL = text("""
WITH params AS (
  SELECT CAST(:embedding AS vector(1536)) AS embedding,
         CAST(:query AS text) AS query,
         CAST(:from_date AS date) AS from_date,
         CAST(:to_date AS date) AS to_date,
         CAST(:focus_id AS uuid) AS focus_id
), eligible AS (
  SELECT d.id,d.kind,d.display_date,d.title,d.content,d.weather,d.published_at,d.first_read_at,
         rd.search_text,
         CASE WHEN p.embedding IS NULL OR rd.embedding IS NULL THEN NULL
              ELSE 1-(rd.embedding <=> p.embedding) END AS similarity
  FROM diaries d
  JOIN diary_recall_documents rd ON rd.user_id=d.user_id AND rd.diary_id=d.id
  CROSS JOIN params p
  WHERE d.user_id=:user_id AND d.record_status='published' AND d.deleted_at IS NULL
    AND d.published_at IS NOT NULL AND d.published_at<=:now
    AND d.kind IN ('welcome','shared_day','capi_day')
    AND (p.from_date IS NULL OR d.display_date>=p.from_date)
    AND (p.to_date IS NULL OR d.display_date<=p.to_date)
    AND (p.focus_id IS NULL OR d.id=p.focus_id)
    AND NOT EXISTS (
      SELECT 1 FROM diary_claim_sources s
      JOIN memory_recall_suppressions x
        ON x.user_id=s.user_id AND x.message_id=s.message_id
      WHERE s.user_id=d.user_id AND s.diary_id=d.id
    )
), ranked AS (
  SELECT *,
    CASE WHEN p.query IS NULL THEN 1.0
         WHEN search_text ILIKE ('%' || p.query || '%') THEN 1.0
         ELSE COALESCE(similarity,0.0) END AS score
  FROM eligible CROSS JOIN params p
  WHERE p.query IS NULL OR search_text ILIKE ('%' || p.query || '%')
        OR COALESCE(similarity,0.0)>=:min_similarity
)
SELECT *, count(*) OVER() AS exact_count
FROM ranked
ORDER BY score DESC,display_date DESC,id DESC
LIMIT :limit
""")


def _vector_literal(vector: list[float] | None) -> str | None:
    if vector is None:
        return None
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


async def recall(
    session: AsyncSession,
    user_id: str | uuid.UUID,
    *,
    query: str | None,
    need: str = "summary",
    from_date: date | None = None,
    to_date: date | None = None,
    focus_id: uuid.UUID | None = None,
    limit: int = 3,
    query_embedding: list[float] | None = None,
) -> dict[str, Any]:
    uid = _uid(str(user_id)) if not isinstance(user_id, uuid.UUID) else user_id
    await privacy.ensure_subject_active(session, uid)
    if from_date and to_date and from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    if focus_id is not None:
        query = None
    effective_limit = max(1, min(limit, MAX_RETURNED))
    nickname = await session.scalar(select(Profile.nickname).where(Profile.id == uid))
    rows = (
        await session.execute(
            _RECALL,
            {
                "user_id": uid,
                "query": query.strip() if query else None,
                "embedding": _vector_literal(query_embedding),
                "min_similarity": 0.25,
                "from_date": from_date,
                "to_date": to_date,
                "focus_id": focus_id,
                "now": datetime.now(timezone.utc),
                "limit": effective_limit,
            },
        )
    ).mappings().all()
    exact_count = int(rows[0]["exact_count"]) if rows else 0
    include_body = need in {"full", "full_card", "quote"}
    items = []
    for row in rows:
        body = naming.render(str(row["content"] or ""), nickname)
        title = naming.render(str(row["title"]), nickname) if row["title"] else None
        items.append(
            {
                "ref": {"type": "diary", "id": str(row["id"])},
                "id": str(row["id"]),
                "kind": row["kind"],
                "display_date": row["display_date"].isoformat(),
                "title": title,
                "excerpt": body if include_body else body[:400],
                "body": body if include_body else None,
                "weather": row["weather"],
                "read": row["first_read_at"] is not None,
            }
        )
    return {
        "status": "ok",
        "matched_count": exact_count,
        "returned_count": len(items),
        "coverage": "complete" if exact_count <= effective_limit else "partial",
        "has_more": exact_count > effective_limit,
        "items": items,
    }
