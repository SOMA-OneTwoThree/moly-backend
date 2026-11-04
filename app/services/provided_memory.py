"""Ephemeral proof for optional memories actually supplied to one foreground reply.

Registry revisions describe bookkeeping as well as semantic changes. Revalidate
provided content and its original evidence instead; never substitute another ID.
Initial capture does not lock. Publication validation protects the supplied
rows until caller commit. No domain writes, provider calls, or persisted proofs.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping, Sequence

from sqlalchemy import text

from app.services import i18n, memory, mem0_ingest, mem0_recall


class ProvidedMemoryError(ValueError):
    """A supplied memory or its evidence cannot be certified."""


@dataclass(frozen=True, slots=True)
class ProvidedSource:
    message_id: int
    source_sender: str
    authority: str
    start_utf8: int
    end_utf8: int
    span_hash: str
    raw_content_hash: str
    kind: str
    message_sender: str
    # Time is checked through the actual rendered memory label, not raw equality.
    source_occurred_at: datetime | None = field(compare=False)


@dataclass(frozen=True, slots=True)
class ProvidedMemory:
    user_id: uuid.UUID
    generation: int
    registry_id: uuid.UUID
    provider_memory_id: uuid.UUID
    provider: str
    collection_version: str
    canonical_text: str
    candidate_hash: str
    exact_text_hash: str
    sources: frozenset[ProvidedSource]
    occurred_at: datetime | None
    language: str | None
    today: date | None
    tz_name: str
    when_label: str | None
    canonical_from_legacy: bool = False


_ROWS = text("""
SELECT r.id AS registry_id,r.provider_memory_id,r.provider,r.collection_version,
       r.canonical_text,r.content_hash,
       COALESCE(r.event_started_at,s.occurred_at,r.created_at) AS occurred_at
FROM mem0_memory_registry r
JOIN chat_contexts c ON c.user_id=r.user_id
JOIN memory_pipeline_states p ON p.user_id=r.user_id
LEFT JOIN LATERAL (
    SELECT MAX(ms.source_occurred_at) AS occurred_at FROM mem0_memory_sources ms
    WHERE ms.registry_id=r.id AND ms.user_id=r.user_id
) s ON true
WHERE r.user_id=:user_id AND c.memory_generation=:generation
  AND p.mode='v2' AND p.bootstrap_status='ready'
  AND r.id=ANY(CAST(:ids AS uuid[])) AND r.collection_version='v2'
  AND r.semantic_status='active' AND r.provider_delete_state='kept'
  AND r.duplicate_of_registry_id IS NULL AND r.superseded_by_registry_id IS NULL
""")
_SOURCES = text("""
SELECT s.registry_id,s.source_message_id,s.source_sender,s.authority,
       s.evidence_start_utf8 AS start_utf8,s.evidence_end_utf8 AS end_utf8,
       s.source_content_hash AS span_hash,s.source_occurred_at,
       m.id AS actual_message_id,m.sender AS message_sender,
       m.kind AS message_kind,m.content AS message_content
FROM mem0_memory_sources s
LEFT JOIN messages m ON m.id=s.source_message_id AND m.user_id=s.user_id
WHERE s.user_id=:user_id AND s.registry_id=ANY(CAST(:ids AS uuid[]))
""")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ids(values) -> tuple[uuid.UUID, ...]:
    try:
        result = tuple(uuid.UUID(str(value)) for value in values)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProvidedMemoryError("provided_memory_identity") from exc
    if len(result) > 256 or len(set(result)) != len(result):
        raise ProvidedMemoryError("provided_memory_identity")
    return result


def _source(row) -> ProvidedSource:
    if (
        row["actual_message_id"] != row["source_message_id"]
        or row["message_kind"] != "normal"
        or row["message_sender"] != "user"
        or row["source_sender"] != "user"
        or row["authority"] not in {"explicit_user", "confirmed_user"}
        or not isinstance(row["message_content"], str)
    ):
        raise ProvidedMemoryError("provided_memory_source_excluded")
    raw = row["message_content"]
    body = memory.sanitize_text(raw).encode("utf-8")
    start, end = row["start_utf8"], row["end_utf8"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body):
        raise ProvidedMemoryError("provided_memory_source_span")
    try:
        body[start:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvidedMemoryError("provided_memory_source_utf8") from exc
    if hashlib.sha256(body[start:end]).hexdigest() != row["span_hash"]:
        raise ProvidedMemoryError("provided_memory_source_changed")
    return ProvidedSource(
        row["source_message_id"],
        row["source_sender"],
        row["authority"],
        start,
        end,
        row["span_hash"],
        _sha(raw),
        row["message_kind"],
        row["message_sender"],
        row["source_occurred_at"],
    )


def certified_legacy_text(value, content_hash: str) -> str:
    """A request-local read adapter, never a canonical column repair."""
    if (not isinstance(value, str) or not mem0_ingest.normalize(value)
            or mem0_ingest.candidate_hash(value) != content_hash):
        raise ProvidedMemoryError("provided_memory_legacy_hash")
    return mem0_ingest.normalize(value)


def _overrides(values, identities):
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ProvidedMemoryError("provided_memory_legacy_identity")
    keys = _ids(values.keys())
    if not set(keys).issubset(identities):
        raise ProvidedMemoryError("provided_memory_legacy_identity")
    return dict(zip(keys, values.values(), strict=True))


async def capture(
    session,
    *,
    user_id,
    generation: int,
    ids: Sequence,
    language: str | None = None,
    today: date | None = None,
    tz_name: str = "Asia/Seoul",
    canonical_overrides: Mapping | None = None,
) -> tuple[ProvidedMemory, ...]:
    """Certify exactly these optional IDs; callers render canonical_text/occurred_at."""
    if type(generation) is not int or generation < 0:
        raise ProvidedMemoryError("provided_memory_generation")
    owner = _ids((user_id,))[0]
    identities = _ids(ids)
    overrides = _overrides(canonical_overrides, identities)
    if not identities:
        return ()
    params = {"user_id": owner, "generation": generation, "ids": identities}
    rows = (await session.execute(_ROWS, params)).mappings().all()
    indexed = {uuid.UUID(str(row["registry_id"])): row for row in rows}
    if len(rows) != len(identities) or set(indexed) != set(identities):
        raise ProvidedMemoryError("provided_memory_unavailable")
    source_rows = (await session.execute(_SOURCES, params)).mappings().all()
    evidence = {identity: set() for identity in identities}
    for row in source_rows:
        identity = uuid.UUID(str(row["registry_id"]))
        if identity not in evidence:
            raise ProvidedMemoryError("provided_memory_source_identity")
        evidence[identity].add(_source(row))
    lang = i18n.resolve(language) if language is not None else None
    result = []
    for identity in identities:
        row = indexed[identity]
        canonical = row["canonical_text"]
        from_legacy = canonical is None
        if from_legacy and identity in overrides:
            canonical = certified_legacy_text(overrides[identity], row["content_hash"])
        if (
            not isinstance(canonical, str)
            or not memory.sanitize_text(canonical)
            or not evidence[identity]
            or mem0_ingest.candidate_hash(canonical) != row["content_hash"]
            or not isinstance(row["provider"], str)
            or not row["provider"]
        ):
            raise ProvidedMemoryError("provided_memory_unverified")
        try:
            provider_id = uuid.UUID(str(row["provider_memory_id"]))
        except (ValueError, TypeError) as exc:
            raise ProvidedMemoryError("provided_memory_provider_identity") from exc
        occurred = row["occurred_at"]
        label = mem0_recall._when_label(occurred, today, tz_name, lang) if lang else None
        result.append(
            ProvidedMemory(
                owner,
                generation,
                identity,
                provider_id,
                row["provider"],
                row["collection_version"],
                canonical,
                row["content_hash"],
                _sha(canonical),
                frozenset(evidence[identity]),
                occurred,
                lang,
                today,
                tz_name,
                label,
                from_legacy,
            )
        )
    return tuple(result)


async def validate(
    session, *, user_id, generation: int, snapshots: Sequence[ProvidedMemory]
) -> bool:
    """Allow bookkeeping and unrelated additions; original evidence must all survive."""
    try:
        snapshots = tuple(snapshots)
    except TypeError:
        return False
    if (
        type(generation) is not int
        or generation < 0
        or any(not isinstance(item, ProvidedMemory) or type(item.canonical_from_legacy) is not bool for item in snapshots)
    ):
        return False
    try:
        owner = _ids((user_id,))[0]
        if any(item.user_id != owner or item.generation != generation for item in snapshots):
            return False
        identities = _ids(item.registry_id for item in snapshots)
        current = await capture(session, user_id=owner, generation=generation, ids=identities,
            canonical_overrides={item.registry_id: item.canonical_text for item in snapshots
                                 if item.canonical_from_legacy})
    except ProvidedMemoryError:
        return False
    indexed = {item.registry_id: item for item in current}
    for expected in snapshots:
        actual = indexed[expected.registry_id]
        if (
            actual.provider_memory_id != expected.provider_memory_id
            or actual.provider != expected.provider
            or actual.collection_version != expected.collection_version
            or actual.canonical_text != expected.canonical_text
            or actual.candidate_hash != expected.candidate_hash
            or actual.exact_text_hash != expected.exact_text_hash
            or not expected.sources
            or not expected.sources.issubset(actual.sources)
        ):
            return False
        if expected.language is None:
            if actual.occurred_at != expected.occurred_at:
                return False
        elif expected.when_label != mem0_recall._when_label(
            expected.occurred_at, expected.today, expected.tz_name, expected.language
        ) or expected.when_label != mem0_recall._when_label(
            actual.occurred_at, expected.today, expected.tz_name, expected.language
        ):
            return False
    return True


# The publication transaction owns these locks until its commit/rollback. Initial
# capture and validate intentionally keep their existing non-locking behavior.
_CURRENT_SOURCE_MESSAGES = text("""
SELECT DISTINCT source_message_id FROM mem0_memory_sources
WHERE user_id=:user_id AND registry_id=ANY(CAST(:ids AS uuid[]))
ORDER BY source_message_id
""")
_LOCK_MESSAGES = text("""
SELECT id FROM messages WHERE user_id=:user_id AND id=ANY(CAST(:message_ids AS bigint[]))
ORDER BY id FOR SHARE
""")
_LOCK_REGISTRY = text("""
SELECT id FROM mem0_memory_registry WHERE user_id=:user_id AND id=ANY(CAST(:ids AS uuid[]))
ORDER BY id FOR UPDATE
""")
_LOCK_SOURCES = text("""
SELECT source_message_id FROM mem0_memory_sources
WHERE user_id=:user_id AND registry_id=ANY(CAST(:ids AS uuid[]))
ORDER BY registry_id,source_message_id,evidence_start_utf8,evidence_end_utf8 FOR SHARE
""")
_SET_PUBLISH_LOCK_TIMEOUT = text("SELECT set_config('lock_timeout',:lock_timeout,true)")
_LOCK_CONFLICT_STATES = frozenset({"55P03", "40P01", "57014"})


def is_lock_conflict(exc) -> bool:
    """Recognize PostgreSQL lock/deadlock/cancel states through driver wrappers."""
    pending, seen = [exc], set()
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if (
            getattr(item, "sqlstate", None) in _LOCK_CONFLICT_STATES
            or getattr(item, "pgcode", None) in _LOCK_CONFLICT_STATES
        ):
            return True
        pending.extend(
            (
                getattr(item, "orig", None),
                getattr(item, "__cause__", None),
                getattr(item, "__context__", None),
            )
        )
    return False


async def validate_for_publish(
    session, *, user_id, generation: int, snapshots: Sequence[ProvidedMemory], deadline: float
) -> bool:
    """Certify the supplied facts through caller commit, with bounded lock waits.

    Message locks precede registry locks. Registry UPDATE blocks new source FK
    inserts. Source SHARE locks then freeze existing references; their actual
    returned message IDs must all be locked, or the whole savepoint is retried.
    Success retains LOCAL lock_timeout for the caller's remaining final writes.
    """
    import asyncio
    import math
    import time

    try:
        snapshots = tuple(snapshots)
    except TypeError:
        return False
    if not snapshots:
        return True
    if (
        type(generation) is not int
        or generation < 0
        or any(not isinstance(item, ProvidedMemory) for item in snapshots)
        or type(deadline) not in (int, float)
        or not math.isfinite(deadline)
    ):
        return False
    try:
        owner = _ids((user_id,))[0]
        identities = tuple(sorted(_ids(item.registry_id for item in snapshots), key=str))
        if any(item.user_id != owner or item.generation != generation for item in snapshots):
            return False
        original_messages = {source.message_id for item in snapshots for source in item.sources}
    except (ProvidedMemoryError, AttributeError):
        return False
    budget = min(1.0, deadline - time.monotonic())
    if budget <= 0:
        return False
    end = time.monotonic() + budget
    timer = asyncio.timeout(budget)
    try:
        async with timer:
            for _ in range(2):
                async with session.begin_nested() as savepoint:
                    remaining = min(end, deadline) - time.monotonic()
                    if remaining <= 0:
                        await savepoint.rollback()
                        return False
                    timeout_ms = max(1, min(250, int(remaining * 1000)))
                    await session.execute(
                        _SET_PUBLISH_LOCK_TIMEOUT, {"lock_timeout": f"{timeout_ms}ms"}
                    )
                    params = {"user_id": owner, "ids": identities}
                    current_sources = (
                        await session.execute(_CURRENT_SOURCE_MESSAGES, params)
                    ).all()
                    wanted_messages = original_messages | {row[0] for row in current_sources}
                    locked = (
                        await session.execute(
                            _LOCK_MESSAGES,
                            {
                                "user_id": owner,
                                "message_ids": sorted(wanted_messages),
                            },
                        )
                    ).all()
                    locked_messages = {row[0] for row in locked}
                    if not original_messages.issubset(locked_messages):
                        await savepoint.rollback()
                        return False
                    locked_registry = (await session.execute(_LOCK_REGISTRY, params)).all()
                    if {row[0] for row in locked_registry} != set(identities):
                        await savepoint.rollback()
                        return False
                    actual_sources = (await session.execute(_LOCK_SOURCES, params)).all()
                    if not {row[0] for row in actual_sources}.issubset(locked_messages):
                        # Never acquire a new message lock after a registry lock.
                        await savepoint.rollback()
                        continue
                    if not await validate(
                        session, user_id=owner, generation=generation, snapshots=snapshots
                    ):
                        await savepoint.rollback()
                        return False
                return True
            return False
    except Exception as exc:
        if is_lock_conflict(exc) or (isinstance(exc, TimeoutError) and timer.expired()):
            return False
        raise
