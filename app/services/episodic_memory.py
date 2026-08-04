"""User-message episodic projection. Raw text remains only in ``messages``."""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import jobs

INDEX_VERSION = "episode-v1"
JOB_EPISODE_EMBED = "memory_episode_embed"

_ENQUEUE_ROW = text("""
INSERT INTO memory_episodic_messages
  (user_id,message_id,source_watermark,content_hash,embedding_model,index_version,
   suppression_generation,indexed_at)
SELECT m.user_id,m.id,:source_watermark,:content_hash,:embedding_model,:index_version,
       COALESCE(c.memory_generation,0),now()
FROM messages m LEFT JOIN chat_contexts c ON c.user_id=m.user_id
WHERE m.user_id=:user_id AND m.id=:message_id AND m.sender='user'
ON CONFLICT (user_id,message_id) DO UPDATE SET
  source_watermark=EXCLUDED.source_watermark,
  content_hash=EXCLUDED.content_hash,
  embedding_model=EXCLUDED.embedding_model,
  index_version=EXCLUDED.index_version,
  suppression_generation=EXCLUDED.suppression_generation,
  embedding_repair_attempts=CASE
    WHEN memory_episodic_messages.content_hash IS DISTINCT FROM EXCLUDED.content_hash
      OR memory_episodic_messages.embedding_model IS DISTINCT FROM EXCLUDED.embedding_model
      OR memory_episodic_messages.index_version IS DISTINCT FROM EXCLUDED.index_version
    THEN 0 ELSE memory_episodic_messages.embedding_repair_attempts END,
  embedding=CASE
    WHEN memory_episodic_messages.content_hash IS DISTINCT FROM EXCLUDED.content_hash
      OR memory_episodic_messages.embedding_model IS DISTINCT FROM EXCLUDED.embedding_model
      OR memory_episodic_messages.index_version IS DISTINCT FROM EXCLUDED.index_version
    THEN NULL ELSE memory_episodic_messages.embedding END,
  indexed_at=now()
RETURNING message_id
""")

_LOAD = text("""
SELECT m.content,e.content_hash,e.embedding_model,e.index_version
FROM memory_episodic_messages e
JOIN messages m ON m.user_id=e.user_id AND m.id=e.message_id AND m.sender='user'
WHERE e.user_id=:user_id AND e.message_id=:message_id
  AND NOT EXISTS (
    SELECT 1 FROM memory_recall_suppressions s
    WHERE s.user_id=e.user_id AND s.message_id=e.message_id
  )
""")

_WRITE = text("""
UPDATE memory_episodic_messages
SET embedding=CAST(:embedding AS vector(1536)),embedding_repair_attempts=0,indexed_at=now()
WHERE user_id=:user_id AND message_id=:message_id AND content_hash=:content_hash
  AND embedding_model=:embedding_model AND index_version=:index_version
  AND NOT EXISTS (
    SELECT 1 FROM memory_recall_suppressions s
    WHERE s.user_id=:user_id AND s.message_id=:message_id
  )
RETURNING message_id
""")


async def enqueue_user_message(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    message_id: int,
    source_watermark: int,
    content: str,
) -> None:
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    row = (
        await session.execute(
            _ENQUEUE_ROW,
            {
                "user_id": user_id,
                "message_id": message_id,
                "source_watermark": source_watermark,
                "content_hash": content_hash,
                "embedding_model": settings.embedder_model,
                "index_version": INDEX_VERSION,
            },
        )
    ).first()
    if row is None:
        raise ValueError("episodic source must be an owned user message")
    await jobs.enqueue(
        session,
        queue="interactive_async",
        job_type=JOB_EPISODE_EMBED,
        dedup_key=(
            f"{INDEX_VERSION}:{settings.embedder_model}:{user_id}:{message_id}:{content_hash}"
        ),
        user_id=user_id,
        payload={"schema_version": INDEX_VERSION, "message_id": message_id},
    )


async def load_for_embedding(
    session: AsyncSession, *, user_id: uuid.UUID, message_id: int
) -> tuple[str, str, str, str] | None:
    row = (
        await session.execute(_LOAD, {"user_id": user_id, "message_id": message_id})
    ).first()
    if row is None:
        return None
    content, expected_hash = str(row[0] or ""), str(row[1])
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_hash:
        return None
    return content, expected_hash, str(row[2]), str(row[3])


async def write_embedding(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    message_id: int,
    content_hash: str,
    embedding_model: str,
    index_version: str,
    vector: Sequence[float],
) -> bool:
    literal = "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"
    row = (
        await session.execute(
            _WRITE,
            {
                "user_id": user_id,
                "message_id": message_id,
                "content_hash": content_hash,
                "embedding_model": embedding_model,
                "index_version": index_version,
                "embedding": literal,
            },
        )
    ).first()
    return row is not None
