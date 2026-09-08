"""Versioned, bounded topic content. Selection never calls an LLM or changes the file."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.app_day import AppDay

CATALOG_PATH = Path(__file__).resolve().parents[1] / "resources/conversation_topics/catalog.json"
MAX_BYTES = 2 * 1024 * 1024
TopicId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Question = Annotated[str, Field(min_length=1, max_length=120)]


class TopicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TopicQuestions(TopicModel):
    ko: Question
    en: Question
    ja: Question

    @field_validator("ko", "en", "ja")
    @classmethod
    def plain_question(cls, value: str) -> str:
        if value != value.strip() or any(unicodedata.category(c) == "Cc" for c in value):
            raise ValueError("question must be trimmed plain text without control characters")
        if "{유저이름}" in value:
            raise ValueError("topic questions cannot contain nickname placeholders")
        if value != unicodedata.normalize("NFC", value):
            raise ValueError("question must use NFC normalization")
        return value

    def for_locale(self, locale: str) -> str:
        return getattr(self, locale if locale in {"ko", "en", "ja"} else "en")


def content_revision(questions: TopicQuestions) -> str:
    payload = json.dumps(
        questions.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class TopicVersion(TopicModel):
    revision: Revision
    questions: TopicQuestions

    @model_validator(mode="after")
    def verify_revision(self):
        if self.revision != content_revision(self.questions):
            raise ValueError("topic revision does not match its questions")
        return self


class TopicDefinition(TopicModel):
    id: TopicId
    category: TopicId
    versions: Annotated[tuple[TopicVersion, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_versions(self):
        if len({version.revision for version in self.versions}) != len(self.versions):
            raise ValueError("duplicate topic revision")
        return self


class TopicPointer(TopicModel):
    topic_id: TopicId
    topic_revision: Revision


class TopicManifest(TopicModel):
    schema_version: Literal[1]
    enabled: bool
    topics: Annotated[tuple[TopicDefinition, ...], Field(min_length=1, max_length=1000)]
    sequence: Annotated[tuple[TopicPointer, ...], Field(min_length=1, max_length=1000)]
    revoked: Annotated[tuple[TopicPointer, ...], Field(max_length=10000)]

    @model_validator(mode="after")
    def valid_references(self):
        if len({topic.id for topic in self.topics}) != len(self.topics):
            raise ValueError("duplicate topic id")
        if len({ref.topic_id for ref in self.sequence}) != len(self.sequence):
            raise ValueError("sequence contains a repeated topic")
        known = {(topic.id, v.revision) for topic in self.topics for v in topic.versions}
        revocations = {(ref.topic_id, ref.topic_revision) for ref in self.revoked}
        if any((ref.topic_id, ref.topic_revision) not in known | revocations
               for ref in self.sequence):
            raise ValueError("sequence references missing content")
        if len(revocations) != len(self.revoked):
            raise ValueError("duplicate revocation")
        # Revocations can outlive a rollback's content, so they need not be in known.
        return self


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON constant")


@dataclass(frozen=True)
class TopicCatalog:
    revision: str
    manifest: TopicManifest
    versions: Mapping[tuple[str, str], TopicQuestions]
    positions: Mapping[str, int]
    revoked: frozenset[tuple[str, str]]

    @classmethod
    def from_bytes(cls, raw: bytes) -> TopicCatalog:
        if len(raw) > MAX_BYTES:
            raise ValueError("topic catalog byte budget exceeded")
        json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                   parse_constant=_invalid_constant)
        manifest = TopicManifest.model_validate_json(raw)
        return cls(
            hashlib.sha256(raw).hexdigest(),
            manifest,
            MappingProxyType({
                (topic.id, version.revision): version.questions
                for topic in manifest.topics for version in topic.versions
            }),
            MappingProxyType({ref.topic_id: i for i, ref in enumerate(manifest.sequence)}),
            frozenset((ref.topic_id, ref.topic_revision) for ref in manifest.revoked),
        )

    @classmethod
    def load(cls, path: Path = CATALOG_PATH) -> TopicCatalog:
        with path.open("rb") as stream:
            return cls.from_bytes(stream.read(MAX_BYTES + 1))

    def questions(self, pointer: TopicPointer) -> TopicQuestions:
        return self.versions[(pointer.topic_id, pointer.topic_revision)]

    def is_revoked(self, topic_id: str, revision: str) -> bool:
        return (topic_id, revision) in self.revoked

    def next_pointer(self, current_topic_id: str | None) -> TopicPointer | None:
        if not self.manifest.enabled:
            return None
        start = -1 if current_topic_id is None else self.positions.get(current_topic_id)
        if start is None:
            # A rolled-back catalog must not reinterpret a newer user's cursor.
            return None
        refs = self.manifest.sequence
        for offset in range(1, len(refs) + 1):
            ref = refs[(start + offset) % len(refs)]
            if ref.topic_id != current_topic_id and not self.is_revoked(
                ref.topic_id, ref.topic_revision
            ):
                return ref
        return None

    def validate_update(self, previous: TopicCatalog, *, allow_reorder: bool = False) -> None:
        before = tuple(ref.topic_id for ref in previous.manifest.sequence)
        after = tuple(ref.topic_id for ref in self.manifest.sequence)
        if not set(before) <= set(after):
            raise ValueError("published topic cursor ids must be preserved")
        if not allow_reorder and after[:len(before)] != before:
            raise ValueError("topic sequence may only append new ids unless reorder is explicit")
        if not previous.revoked <= self.revoked:
            raise ValueError("revocations cannot be removed")
        for key, questions in previous.versions.items():
            if key not in self.versions and key in self.revoked:
                continue
            if self.versions.get(key) != questions:
                raise ValueError("published topic versions must be preserved unless revoked")


def entry_expiration(now: datetime, timezone_name: str) -> datetime:
    return max(AppDay.at(now, timezone_name).ends_at, now + timedelta(minutes=30))
