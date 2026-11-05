"""Server-only source identity; positions and rendered text never grant coverage.

HistoryEntry deliberately contains only provider wire keys. Its provenance is an
attribute, not a mapping key, and survives trusted text transformations/copies.
Missing provenance is mandatory input, including legacy callers.
"""
from __future__ import annotations

from app.services import message_content

import hashlib
import uuid
from dataclasses import dataclass, replace
from typing import TypeVar


class ProvenanceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceRef:
    user_id: uuid.UUID
    message_id: int
    memory_generation: int
    raw_content_hash: str

    def __post_init__(self):
        if (not isinstance(self.user_id, uuid.UUID)
                or type(self.message_id) is not int or self.message_id <= 0
                or type(self.memory_generation) is not int or self.memory_generation < 0
                or not isinstance(self.raw_content_hash, str) or len(self.raw_content_hash) != 64
                or any(c not in "0123456789abcdef" for c in self.raw_content_hash)):
            raise ProvenanceError("invalid_source_identity")

    @classmethod
    def from_message(cls, message, *, user_id, generation: int):
        if (type(message.id) is not int or message.id <= 0
                or type(generation) is not int or generation < 0):
            raise ProvenanceError("invalid_source_identity")
        body = message.content or ""
        if hasattr(message, "content_ciphertext") or hasattr(message, "content_chars"):
            body = message_content.read_message_content(user_id=user_id, legacy_text=message.content,
                ciphertext=getattr(message, "content_ciphertext", None), chars=getattr(message, "content_chars", None))
        return cls(uuid.UUID(str(user_id)), message.id, generation,
                   hashlib.sha256(body.encode("utf-8")).hexdigest())


@dataclass(frozen=True, slots=True)
class VerifiedCoverage:
    user_id: uuid.UUID
    memory_generation: int
    checkpoint_id: uuid.UUID
    through_message_id: int
    source_hash: str
    policy_excluded_ids: frozenset[int] = frozenset()
    # The repository grants an interval. The assembler binds only actual fetched
    # original rows; runtime permission is exact SourceRef membership, not <= N.
    source_refs: frozenset[SourceRef] = frozenset()
    after_message_id: int = 0

    def __post_init__(self):
        if (not isinstance(self.user_id, uuid.UUID) or not isinstance(self.checkpoint_id, uuid.UUID)
                or type(self.memory_generation) is not int or self.memory_generation < 0
                or type(self.through_message_id) is not int or self.through_message_id <= 0
                or type(self.after_message_id) is not int
                or not 0 <= self.after_message_id < self.through_message_id
                or not isinstance(self.source_hash, str) or len(self.source_hash) != 64
                or any(c not in "0123456789abcdef" for c in self.source_hash)
                or not isinstance(self.source_refs, frozenset)
                or not isinstance(self.policy_excluded_ids, frozenset)
                or any(type(i) is not int or i <= self.after_message_id or i > self.through_message_id
                       for i in self.policy_excluded_ids)):
            raise ProvenanceError("invalid_coverage_identity")

    def covers(self, source: SourceRef) -> bool:
        if not isinstance(source, SourceRef):
            raise ProvenanceError("invalid_source_identity")
        if (source.user_id != self.user_id
                or source.memory_generation != self.memory_generation):
            raise ProvenanceError("stale_source_identity")
        if type(source.message_id) is not int or source.message_id <= 0:
            raise ProvenanceError("invalid_source_identity")
        return (source in self.source_refs
                and self.after_message_id < source.message_id <= self.through_message_id)

    def bind_sources(self, sources) -> VerifiedCoverage:
        exact = []
        for source in sources:
            # Validate owner/generation even for uncheckpointed rows.
            self.covers(source)
            if self.after_message_id < source.message_id <= self.through_message_id:
                exact.append(source)
        return replace(self, source_refs=frozenset(exact))


class HistoryEntry(dict):
    """role/content mapping with a server-owned, non-wire original identity."""
    __slots__ = ("_source_ref",)

    def __init__(self, role: str, content: str, *, source_ref: SourceRef | None = None):
        super().__init__(role=role, content=content)
        self._source_ref = source_ref

    @property
    def source_ref(self) -> SourceRef | None:
        return self._source_ref

    def copy(self):
        return HistoryEntry(self["role"], self["content"], source_ref=self.source_ref)


def copy_entry(entry) -> HistoryEntry:
    return HistoryEntry(entry["role"], entry["content"],
                        source_ref=getattr(entry, "source_ref", None))


T = TypeVar("T")


def without_covered(entries: list[T], coverage: VerifiedCoverage | None) -> list[T]:
    """The coverage-based budget compaction: filter individually granted sources.

    Mandatory items retain their object, order and contents. Tools/current input/
    volatile instructions do not acquire permission by matching a raw string.
    Only this filter may treat raw history as summarized. The one other reduction
    is chat's pressure fallback after a summary wait could not complete: it drops
    the oldest sourced raw history for that request only, adds the "earlier
    conversation may be missing" notice, and never grants coverage or moves the anchor.
    """
    if coverage is None:
        return list(entries)
    if not isinstance(coverage, VerifiedCoverage):
        raise ProvenanceError("invalid_coverage_identity")
    kept = []
    for entry in entries:
        source = getattr(entry, "source_ref", None)
        if source is not None and not coverage.covers(source):
            kept.append(entry)
        elif source is None:
            kept.append(entry)
    return kept


def wire_messages(entries) -> list[dict]:
    """Explicit whitelist: internal provenance never enters a provider payload."""
    return [{"role": entry["role"], "content": entry["content"]} for entry in entries]
