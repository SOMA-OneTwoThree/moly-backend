"""Suppression-safe diary recall projection and provenance writes."""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import jobs, memory_embeddings

INDEX_VERSION = "diary-recall-v1"
JOB_DIARY_RECALL_EMBED = "diary_recall_embed"

_MESSAGES = text(
    "SELECT id,sender,content FROM messages WHERE user_id=:user_id AND id IN :ids"
).bindparams(bindparam("ids", expanding=True))

_INSERT_SOURCE = text("""
INSERT INTO diary_claim_sources(user_id,diary_id,message_id,source_hash)
VALUES (:user_id,:diary_id,:message_id,:source_hash)
ON CONFLICT (user_id,diary_id,message_id) DO UPDATE SET source_hash=EXCLUDED.source_hash
""")

_UPSERT_DOCUMENT = text("""
INSERT INTO diary_recall_documents
  (user_id,diary_id,search_text,source_hash,embedding_model,
   suppression_generation,index_version,updated_at)
SELECT d.user_id,d.id,
       trim(concat_ws(E'\n',d.title,d.content)),
       encode(digest(concat_ws(E'\n',d.title,d.content),'sha256'),'hex'),:embedding_model,
       COALESCE(c.memory_generation,0),:index_version,now()
FROM diaries d
LEFT JOIN chat_contexts c ON c.user_id=d.user_id
WHERE d.user_id=:user_id AND d.id=:diary_id
  AND d.record_status='published' AND d.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM privacy_subject_barriers b WHERE b.user_id=d.user_id)
ON CONFLICT (user_id,diary_id) DO UPDATE SET
  search_text=EXCLUDED.search_text,
  source_hash=EXCLUDED.source_hash,
  embedding_model=EXCLUDED.embedding_model,
  suppression_generation=EXCLUDED.suppression_generation,
  index_version=EXCLUDED.index_version,
  embedding_repair_attempts=CASE
    WHEN diary_recall_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
      OR diary_recall_documents.embedding_model IS DISTINCT FROM EXCLUDED.embedding_model
      OR diary_recall_documents.index_version IS DISTINCT FROM EXCLUDED.index_version
    THEN 0 ELSE diary_recall_documents.embedding_repair_attempts END,
  embedding=CASE
    WHEN diary_recall_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
      OR diary_recall_documents.embedding_model IS DISTINCT FROM EXCLUDED.embedding_model
      OR diary_recall_documents.index_version IS DISTINCT FROM EXCLUDED.index_version
    THEN NULL ELSE diary_recall_documents.embedding END,
  updated_at=now()
RETURNING source_hash,suppression_generation
""")

_LOAD_DOCUMENT = text("""
SELECT rd.search_text,rd.source_hash,rd.suppression_generation,
       rd.embedding_model,rd.index_version,
       COALESCE(c.memory_generation,0) AS current_generation
FROM diary_recall_documents rd
JOIN diaries d ON d.user_id=rd.user_id AND d.id=rd.diary_id
LEFT JOIN chat_contexts c ON c.user_id=rd.user_id
WHERE rd.user_id=:user_id AND rd.diary_id=:diary_id
  AND d.record_status='published' AND d.deleted_at IS NULL
  AND rd.suppression_generation=COALESCE(c.memory_generation,0)
  AND NOT EXISTS (
    SELECT 1 FROM privacy_subject_barriers b WHERE b.user_id=rd.user_id
  )
  AND NOT EXISTS (
    SELECT 1 FROM diary_claim_sources s
    JOIN memory_recall_suppressions x
      ON x.user_id=s.user_id AND x.message_id=s.message_id
    WHERE s.user_id=rd.user_id AND s.diary_id=rd.diary_id
  )
""")

_WRITE_EMBEDDING = text("""
UPDATE diary_recall_documents
SET embedding=CAST(:embedding AS vector(1536)),embedding_repair_attempts=0,updated_at=now()
WHERE user_id=:user_id AND diary_id=:diary_id
  AND source_hash=:source_hash
  AND suppression_generation=:generation
  AND embedding_model=:embedding_model AND index_version=:index_version
RETURNING diary_id
""")


async def record_diary_sources(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    diary_id: uuid.UUID,
    message_ids: Sequence[int],
) -> None:
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return
    rows = (await session.execute(_MESSAGES, {"user_id": user_id, "ids": ids})).mappings().all()
    found = {int(row["id"]): row for row in rows}
    if set(found) != set(ids) or any(row["sender"] != "user" for row in rows):
        raise ValueError("diary provenance must contain only owned user messages")
    for message_id in ids:
        content = found[message_id]["content"] or ""
        await session.execute(
            _INSERT_SOURCE,
            {
                "user_id": user_id,
                "diary_id": diary_id,
                "message_id": message_id,
                "source_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            },
        )


async def upsert_diary_recall_document(
    session: AsyncSession, *, user_id: uuid.UUID, diary_id: uuid.UUID
) -> None:
    row = (
        await session.execute(
            _UPSERT_DOCUMENT,
            {
                "user_id": user_id,
                "diary_id": diary_id,
                "embedding_model": settings.embedder_model,
                "index_version": INDEX_VERSION,
            },
        )
    ).first()
    if row is None:
        return
    await jobs.enqueue(
        session,
        queue="content",
        job_type=JOB_DIARY_RECALL_EMBED,
        dedup_key=(
            f"{INDEX_VERSION}:{settings.embedder_model}:{diary_id}:{row[0]}:{int(row[1])}"
        ),
        user_id=user_id,
        payload={"schema_version": INDEX_VERSION, "diary_id": str(diary_id)},
    )


async def embed_diary_document(
    session: AsyncSession, *, user_id: uuid.UUID, diary_id: uuid.UUID
) -> tuple[str, int, str, str, str] | None:
    """Load an eligible document. The caller embeds after closing this session."""
    row = (
        await session.execute(_LOAD_DOCUMENT, {"user_id": user_id, "diary_id": diary_id})
    ).mappings().first()
    if row is None:
        return None
    return (
        str(row["search_text"]),
        int(row["suppression_generation"]),
        str(row["source_hash"]),
        str(row["embedding_model"]),
        str(row["index_version"]),
    )


async def write_diary_embedding(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    diary_id: uuid.UUID,
    source_hash: str,
    generation: int,
    embedding_model: str,
    index_version: str,
    vector: Sequence[float],
) -> bool:
    literal = "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"
    row = (
        await session.execute(
            _WRITE_EMBEDDING,
            {
                "user_id": user_id,
                "diary_id": diary_id,
                "source_hash": source_hash,
                "generation": generation,
                "embedding_model": embedding_model,
                "index_version": index_version,
                "embedding": literal,
            },
        )
    ).first()
    return row is not None


async def embed_text(value: str) -> list[float]:
    return await memory_embeddings.embed_query(value)
