"""Shared embeddings for approved public copy; owned diary projections stay private."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.advisory_lock import advisory_xact_lock
from app.services import config_store, jobs

MODE_KEY = "diary_embedding_storage_mode"
JOB_DIARY_TEMPLATE_EMBED = "diary_template_embed"
INDEX_VERSION = "diary-recall-v1"
DIMENSIONS = 1536


@dataclass(frozen=True)
class SharedDiaryRoute:
    user_id: uuid.UUID
    diary_id: uuid.UUID


def approved_welcome_pairs() -> list[tuple[str, str]]:
    # Lazy import: diary publication itself imports diary_recall_repo.
    from app.services import diary

    result = []
    for value in (diary._WELCOME_CONTENT, diary._WELCOME_CONTENT_EN, diary._WELCOME_CONTENT_JA):
        title, separator, body = value.partition("\n\n")
        if not separator:
            raise ValueError("invalid approved welcome copy")
        result.append((title, body))
    return result


async def storage_mode(session: AsyncSession) -> str:
    values = await config_store.get_config_values(session, [MODE_KEY])
    mode = values.get(MODE_KEY, "private")
    if mode not in ("private", "dual", "shared"):
        raise ValueError("invalid diary embedding storage mode")
    return mode


_OWNED = """
SELECT rd.shared_embedding_id,rd.embedding IS NOT NULL AS private_ready,
       s.id AS valid_shared_id,s.embedding IS NOT NULL AS shared_ready,
       ((d.kind='capi_day' AND d.source='preset' AND d.title IS NULL AND d.content=m.content)
        OR (d.kind='welcome' AND d.source='welcome' AND EXISTS (
          SELECT 1 FROM unnest(CAST(:titles AS text[]),CAST(:bodies AS text[])) w(title,body)
          WHERE d.title=w.title AND d.content=w.body))) AS approved
FROM diary_recall_documents rd
JOIN diaries d ON d.user_id=rd.user_id AND d.id=rd.diary_id
LEFT JOIN moly_life_ments m ON m.id=d.preset_ment_id
LEFT JOIN chat_contexts c ON c.user_id=rd.user_id
LEFT JOIN diary_template_embeddings s ON s.id=rd.shared_embedding_id
  AND d.kind IN ('welcome','capi_day')
  AND s.source_hash=rd.source_hash AND s.embedding_model=rd.embedding_model
  AND s.index_version=rd.index_version AND s.dimensions=1536 AND s.search_text=rd.search_text
WHERE rd.user_id=:user_id AND rd.diary_id=:diary_id
  AND d.record_status='published' AND d.deleted_at IS NULL
  AND rd.suppression_generation=COALESCE(c.memory_generation,0)
  AND rd.search_text=trim(concat_ws(E'\\n',d.title,d.content))
  AND rd.source_hash=encode(digest(concat_ws(E'\\n',d.title,d.content),'sha256'),'hex')
  AND NOT EXISTS (SELECT 1 FROM privacy_subject_barriers b
                  WHERE b.user_id=rd.user_id AND b.state <> 'active')
"""

_INSERT = text("""
INSERT INTO diary_template_embeddings(source_hash,embedding_model,index_version,dimensions,search_text,embedding)
SELECT source_hash,embedding_model,index_version,1536,search_text,embedding
FROM diary_recall_documents WHERE user_id=:user_id AND diary_id=:diary_id
ON CONFLICT(source_hash,embedding_model,index_version,dimensions) DO NOTHING
""")

_SEED = text("""
UPDATE diary_template_embeddings s SET embedding=rd.embedding,updated_at=now()
FROM diary_recall_documents rd
WHERE rd.user_id=:user_id AND rd.diary_id=:diary_id
  AND s.source_hash=rd.source_hash AND s.embedding_model=rd.embedding_model
  AND s.index_version=rd.index_version AND s.dimensions=1536 AND s.search_text=rd.search_text
  AND s.embedding IS NULL AND rd.embedding IS NOT NULL
""")

_ATTACH = text("""
UPDATE diary_recall_documents rd
SET shared_embedding_id=s.id,
    embedding=CASE WHEN :materialize AND rd.embedding IS NULL THEN s.embedding ELSE rd.embedding END,
    embedding_repair_attempts=0,updated_at=now()
FROM diary_template_embeddings s
WHERE rd.user_id=:user_id AND rd.diary_id=:diary_id
  AND s.source_hash=rd.source_hash AND s.embedding_model=rd.embedding_model
  AND s.index_version=rd.index_version AND s.dimensions=1536 AND s.search_text=rd.search_text
  AND (rd.embedding IS NULL OR rd.embedding=s.embedding)
RETURNING s.id,s.embedding IS NOT NULL AS ready
""")


async def _owned(session: AsyncSession, user_id: uuid.UUID, diary_id: uuid.UUID, *, lock=False):
    pairs = approved_welcome_pairs()
    query = _OWNED + (" FOR UPDATE OF rd" if lock else "")
    return (await session.execute(text(query), {
        "user_id": user_id, "diary_id": diary_id,
        "titles": [p[0] for p in pairs], "bodies": [p[1] for p in pairs],
    })).mappings().first()


async def should_route(session: AsyncSession, *, user_id: uuid.UUID, diary_id: uuid.UUID) -> bool:
    mode = await storage_mode(session)
    row = await _owned(session, user_id, diary_id)
    if row is None:
        return False
    if row["valid_shared_id"]:
        return bool(row["shared_ready"]) or mode != "dual"
    return mode == "shared" and bool(row["approved"])


async def enqueue_shared(session: AsyncSession, shared_id: uuid.UUID) -> None:
    await jobs.enqueue(
        session, queue=jobs.QUEUE_CONTENT, job_type=JOB_DIARY_TEMPLATE_EMBED,
        dedup_key=f"diary-template:{shared_id}", user_id=None,
        payload={"schema_version": INDEX_VERSION, "shared_id": str(shared_id)},
    )


async def ensure_for_diary(
    session: AsyncSession, *, user_id: uuid.UUID, diary_id: uuid.UUID, mode: str | None = None,
) -> bool:
    """Route approved input or an existing valid reference. False keeps private embedding work.

    A ready canonical is immutable. A historical private variant never gets replaced by it.
    The user lock and row lock serialize this with publication/deletion and cutover tooling.
    """
    await advisory_xact_lock(session, user_id)
    if mode is None:
        mode = await storage_mode(session)
    elif mode not in ("private", "dual", "shared"):
        raise ValueError("invalid diary embedding storage mode")
    row = await _owned(session, user_id, diary_id, lock=True)
    if row is None:
        return False
    params = {"user_id": user_id, "diary_id": diary_id}
    if row["shared_embedding_id"] and not row["valid_shared_id"]:
        await session.execute(text("""
          UPDATE diary_recall_documents SET shared_embedding_id=NULL
          WHERE user_id=:user_id AND diary_id=:diary_id
        """), params)
    if not row["valid_shared_id"] and (mode == "private" or not row["approved"]):
        return False
    if not row["valid_shared_id"]:
        await session.execute(_INSERT, params)
    # No external call: prefer a valid existing private vector when canonical is still pending.
    await session.execute(_SEED, params)
    attached = (await session.execute(_ATTACH, {**params, "materialize": mode != "shared"})).first()
    if attached is None:
        # Existing variant differs: keep its original private vector and detach any stale pointer.
        await session.execute(text("""
          UPDATE diary_recall_documents SET shared_embedding_id=NULL
          WHERE user_id=:user_id AND diary_id=:diary_id AND embedding IS NOT NULL
        """), params)
        if not row["private_ready"]:
            raise ValueError("shared embedding input mismatch")
        return True
    shared_id, ready = attached
    if not ready:
        await enqueue_shared(session, shared_id)
    return bool(ready) or mode != "dual"


async def load_shared(session: AsyncSession, shared_id: uuid.UUID):
    return (await session.execute(text("""
      SELECT id,search_text,source_hash,embedding_model,index_version,dimensions
      FROM diary_template_embeddings WHERE id=:id AND embedding IS NULL
    """), {"id": shared_id})).mappings().first()


async def write_shared(session: AsyncSession, *, source: dict, vector: list[float]) -> bool:
    if len(vector) != DIMENSIONS:
        raise ValueError("invalid shared embedding dimensions")
    literal = "[" + ",".join(format(float(v), ".9g") for v in vector) + "]"
    row = (await session.execute(text("""
      UPDATE diary_template_embeddings
      SET embedding=CAST(:vector AS vector(1536)),embedding_repair_attempts=0,updated_at=now()
      WHERE id=:id AND embedding IS NULL AND source_hash=:source_hash
        AND embedding_model=:embedding_model AND index_version=:index_version
        AND search_text=:search_text AND dimensions=:dimensions
      RETURNING id
    """), {**source, "vector": literal})).first()
    return row is not None
