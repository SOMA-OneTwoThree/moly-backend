"""Typed topic references. Authored questions are never accepted from clients."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel

from app.schemas.common import StrictResponse, UtcDatetime


class TopicReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer_id: UUID
    offer_sequence: int = Field(gt=0)
    topic_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    topic_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    locale: Literal["ko", "en", "ja"]


class BannerClientContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placement: Literal["home_blind"]
    schema_version: Literal[1]
    platform: Literal["ios", "android"]
    app_version: str = Field(min_length=1, max_length=64)
    capabilities: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]] = Field(
        max_length=32,
    )


class PrepareTopicRequest(BannerClientContext):
    banner_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    topic_ref: TopicReference


class TopicEntryBase(StrictResponse):
    entry_id: UUID
    offer_id: UUID
    created_at: UtcDatetime
    expires_at: UtcDatetime


class PendingTopicEntry(TopicEntryBase):
    state: Literal["pending"]
    locale: Literal["ko", "en", "ja"]
    content: str = Field(min_length=1, max_length=120)


class CommittedTopicEntry(TopicEntryBase):
    state: Literal["committed"]
    message_id: str = Field(pattern=r"^\d+$")


class SupersededTopicEntry(TopicEntryBase):
    state: Literal["superseded"]


TopicEntry = Annotated[
    PendingTopicEntry | CommittedTopicEntry | SupersededTopicEntry,
    Field(discriminator="state"),
]


class TopicEntryResponse(RootModel[TopicEntry]):
    """Concrete response model with a state-discriminated JSON root."""


def entry_response(entry, *, locale: Literal["ko", "en", "ja"], now: datetime):
    base = dict(entry_id=entry.id, offer_id=entry.offer_id,
                created_at=entry.created_at, expires_at=entry.expires_at)
    # A safety-first reply may have omitted the question, but its conversation
    # still exists and must reopen without treating it as an unanswered failure.
    message_id = entry.committed_message_id or entry.first_user_message_id
    if message_id is not None:
        return CommittedTopicEntry(**base, state="committed",
                                   message_id=str(message_id))
    if entry.state == "pending" and now < entry.expires_at:
        return PendingTopicEntry(**base, state="pending", locale=locale,
                                 content=entry.questions[locale])
    return SupersededTopicEntry(**base, state="superseded")
