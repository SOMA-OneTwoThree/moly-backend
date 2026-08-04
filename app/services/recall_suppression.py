"""Exact message-level recall suppression shared by transcript, episode, diary and focus."""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

_BY_FACTS = text("""
SELECT DISTINCT e.source_id AS message_id,tm.source_watermark
FROM memory_evidence e
JOIN memory_source_turn_messages tm
  ON tm.user_id=e.user_id AND tm.message_id=e.source_id
WHERE e.user_id=:user_id AND e.fact_id IN :fact_ids
""").bindparams(bindparam("fact_ids", expanding=True))

_BY_PREDICATE = text("""
SELECT DISTINCT e.source_id AS message_id,tm.source_watermark
FROM memory_facts f
JOIN memory_evidence e ON e.user_id=f.user_id AND e.fact_id=f.id
JOIN memory_source_turn_messages tm
  ON tm.user_id=e.user_id AND tm.message_id=e.source_id
WHERE f.user_id=:user_id AND f.predicate=:predicate
""")

_ALL_BEFORE = text("""
SELECT DISTINCT message_id,source_watermark
FROM memory_source_turn_messages
WHERE user_id=:user_id AND source_watermark<=:cut_watermark
""")

_TURN_MESSAGES = text("""
SELECT DISTINCT tm.message_id,tm.source_watermark,m.content
FROM memory_source_turn_messages tm
JOIN messages m ON m.user_id=tm.user_id AND m.id=tm.message_id
WHERE tm.user_id=:user_id AND tm.source_watermark IN :watermarks
""").bindparams(bindparam("watermarks", expanding=True))

_INSERT_OPERATION = text("""
INSERT INTO memory_suppression_operations(id,user_id,cut_watermark,future_learning,scope,reason)
VALUES (:id,:user_id,:cut_watermark,:future_learning,:scope,:reason)
""")

_INSERT = text("""
INSERT INTO memory_recall_suppressions
  (user_id,operation_id,message_id,source_watermark,source_hash,reason)
VALUES (:user_id,:operation_id,:message_id,:source_watermark,:source_hash,:reason)
ON CONFLICT DO NOTHING
""")

_SUPPRESSED_IDS = text("""
SELECT DISTINCT message_id FROM memory_recall_suppressions
WHERE user_id=:user_id AND message_id IN :message_ids
""").bindparams(bindparam("message_ids", expanding=True))

_INVALIDATE_FOCUS = text("DELETE FROM conversation_focus WHERE user_id=:user_id")
_REDACT_REFS = text("""
UPDATE chat_response_references r SET
  state='unavailable',diary_id=NULL,rendered_metadata='{}'::jsonb,
  redacted_at=now(),redaction_reason='memory_suppressed'
WHERE r.user_id=:user_id AND r.diary_id IN (
  SELECT s.diary_id FROM diary_claim_sources s
  JOIN memory_recall_suppressions x
    ON x.user_id=s.user_id AND x.message_id=s.message_id
  WHERE s.user_id=:user_id
)
""")


async def apply(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    operation_id: uuid.UUID,
    scope: str,
    cut_watermark: int,
    future_learning: str,
    fact_ids: Sequence[uuid.UUID] = (),
    predicate: str | None = None,
) -> int:
    if scope == "fact":
        if not fact_ids:
            return 0
        base = (
            await session.execute(
                _BY_FACTS, {"user_id": user_id, "fact_ids": list(fact_ids)}
            )
        ).mappings().all()
    elif scope == "predicate":
        base = (
            await session.execute(
                _BY_PREDICATE, {"user_id": user_id, "predicate": predicate}
            )
        ).mappings().all()
    elif scope == "all":
        base = (
            await session.execute(
                _ALL_BEFORE, {"user_id": user_id, "cut_watermark": cut_watermark}
            )
        ).mappings().all()
    else:
        raise ValueError(f"unsupported suppression scope: {scope}")
    watermarks = sorted({int(row["source_watermark"]) for row in base})
    if not watermarks:
        return 0
    # Suppress the user assertion and every assistant/greeting echo from the same committed turn.
    messages = (
        await session.execute(
            _TURN_MESSAGES, {"user_id": user_id, "watermarks": watermarks}
        )
    ).mappings().all()
    await session.execute(
        _INSERT_OPERATION,
        {
            "id": operation_id,
            "user_id": user_id,
            "cut_watermark": cut_watermark,
            "future_learning": future_learning,
            "scope": scope,
            "reason": "user_forget",
        },
    )
    rows = [
        {
            "user_id": user_id,
            "operation_id": operation_id,
            "message_id": int(row["message_id"]),
            "source_watermark": int(row["source_watermark"]),
            "source_hash": hashlib.sha256(str(row["content"] or "").encode("utf-8")).hexdigest(),
            "reason": "same_turn_echo" if int(row["message_id"]) not in {
                int(item["message_id"]) for item in base
            } else "user_assertion",
        }
        for row in messages
    ]
    if rows:
        await session.execute(_INSERT, rows)
    await session.execute(_INVALIDATE_FOCUS, {"user_id": user_id})
    await session.execute(_REDACT_REFS, {"user_id": user_id})
    return len(rows)


async def suppressed_message_ids(
    session: AsyncSession, *, user_id: uuid.UUID, message_ids: Sequence[int]
) -> set[int]:
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return set()
    rows = (
        await session.execute(
            _SUPPRESSED_IDS, {"user_id": user_id, "message_ids": ids}
        )
    ).all()
    return {int(row[0]) for row in rows}
