"""Bounded reconciliation for missing episode/diary recall embeddings.

The source rows are authoritative metadata; vectors are rebuildable. A terminal async job is never
resurrected. Instead, a missing row receives at most three distinct repair jobs for the same source
version. Selection, attempt increment, and enqueue share one transaction.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import jobs
from app.services.diary_recall_repo import JOB_DIARY_RECALL_EMBED
from app.services.episodic_memory import JOB_EPISODE_EMBED

MAX_REPAIR_ATTEMPTS = 3

_EPISODES = text("""
WITH candidate AS (
  SELECT e.user_id,e.message_id
  FROM memory_episodic_messages e
  JOIN messages m ON m.user_id=e.user_id AND m.id=e.message_id AND m.sender='user'
  WHERE e.embedding IS NULL AND e.embedding_repair_attempts<:max_attempts
    AND NOT EXISTS (SELECT 1 FROM privacy_subject_barriers b
                    WHERE b.user_id=e.user_id AND b.state <> 'active')
    AND NOT EXISTS (
      SELECT 1 FROM memory_recall_suppressions s
      WHERE s.user_id=e.user_id AND s.message_id=e.message_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM async_jobs j
      WHERE j.user_id=e.user_id AND j.job_type='memory_episode_embed'
        AND j.state IN ('ready','running')
        AND j.payload->>'message_id'=e.message_id::text
    )
  ORDER BY e.indexed_at,e.user_id,e.message_id
  FOR UPDATE OF e SKIP LOCKED
  LIMIT :batch_size
)
UPDATE memory_episodic_messages e
SET embedding_repair_attempts=e.embedding_repair_attempts+1,indexed_at=now()
FROM candidate c
WHERE e.user_id=c.user_id AND e.message_id=c.message_id
RETURNING e.user_id,e.message_id,e.content_hash,e.embedding_model,e.index_version,
          e.embedding_repair_attempts
""")

_DIARIES = text("""
WITH candidate AS (
  SELECT rd.user_id,rd.diary_id
  FROM diary_recall_documents rd
  JOIN diaries d ON d.user_id=rd.user_id AND d.id=rd.diary_id
  LEFT JOIN chat_contexts c ON c.user_id=rd.user_id
  WHERE rd.embedding IS NULL AND rd.embedding_repair_attempts<:max_attempts
    AND d.record_status='published' AND d.deleted_at IS NULL
    AND rd.suppression_generation=COALESCE(c.memory_generation,0)
    AND NOT EXISTS (SELECT 1 FROM privacy_subject_barriers b
                    WHERE b.user_id=rd.user_id AND b.state <> 'active')
    AND NOT EXISTS (
      SELECT 1 FROM diary_claim_sources s
      JOIN memory_recall_suppressions x
        ON x.user_id=s.user_id AND x.message_id=s.message_id
      WHERE s.user_id=rd.user_id AND s.diary_id=rd.diary_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM async_jobs j
      WHERE j.user_id=rd.user_id AND j.job_type='diary_recall_embed'
        AND j.state IN ('ready','running')
        AND j.payload->>'diary_id'=rd.diary_id::text
    )
  ORDER BY rd.updated_at,rd.user_id,rd.diary_id
  FOR UPDATE OF rd SKIP LOCKED
  LIMIT :batch_size
)
UPDATE diary_recall_documents rd
SET embedding_repair_attempts=rd.embedding_repair_attempts+1,updated_at=now()
FROM candidate c
WHERE rd.user_id=c.user_id AND rd.diary_id=c.diary_id
RETURNING rd.user_id,rd.diary_id,rd.source_hash,rd.embedding_model,rd.index_version,
          rd.suppression_generation,rd.embedding_repair_attempts
""")


async def enqueue_missing(session: AsyncSession, *, batch_size: int = 100) -> int:
    """Enqueue bounded repairs and return their count. The caller owns commit/rollback."""
    limit = max(1, min(int(batch_size), 500))
    params = {"batch_size": limit, "max_attempts": MAX_REPAIR_ATTEMPTS}
    episodes = (await session.execute(_EPISODES, params)).mappings().all()
    diaries = (await session.execute(_DIARIES, params)).mappings().all()
    created = 0
    for row in episodes:
        job_id = await jobs.enqueue(
            session,
            queue="interactive_async",
            job_type=JOB_EPISODE_EMBED,
            dedup_key=(
                "repair:episode:"
                f"{row['index_version']}:{row['embedding_model']}:{row['user_id']}:"
                f"{row['message_id']}:{row['content_hash']}:{row['embedding_repair_attempts']}"
            ),
            user_id=row["user_id"],
            payload={"schema_version": row["index_version"], "message_id": row["message_id"]},
        )
        created += job_id is not None
    for row in diaries:
        job_id = await jobs.enqueue(
            session,
            queue="content",
            job_type=JOB_DIARY_RECALL_EMBED,
            dedup_key=(
                "repair:diary:"
                f"{row['index_version']}:{row['embedding_model']}:{row['diary_id']}:"
                f"{row['source_hash']}:{row['suppression_generation']}:"
                f"{row['embedding_repair_attempts']}"
            ),
            user_id=row["user_id"],
            payload={"schema_version": row["index_version"], "diary_id": str(row["diary_id"])},
        )
        created += job_id is not None
    return created
