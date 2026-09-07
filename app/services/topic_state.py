"""Transactional topic progress. Callers own commit and privacy admission.

Every writer shares the chat user's advisory lock. Completing an offer does not
allocate its successor: the next home resolve does that exactly once.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.advisory_lock import advisory_xact_lock
from app.core.app_day import AppDay
from app.core.errors import AppError
from app.models.topic import ChatTopicEntry, UserTopicState
from app.services.topic_catalog import TopicCatalog, entry_expiration, should_advance

PLACEMENT = "home_blind"


async def resolve_offer(
    session: AsyncSession, user_id: uuid.UUID, catalog: TopicCatalog, day: AppDay,
) -> UserTopicState | None:
    await advisory_xact_lock(session, user_id)
    state = await session.get(UserTopicState, (user_id, PLACEMENT), populate_existing=True)
    if not catalog.manifest.enabled:
        return None
    if state is not None:
        # An older deployment must not reset progress created by a newer catalog.
        if state.topic_id not in catalog.positions:
            return None
        advance = should_advance(
            completed=state.completed, high_watermark=state.day_high_watermark,
            local_date=day.local_date,
        ) or catalog.is_revoked(state.topic_id, state.topic_revision)
        if not advance:
            return state
    pointer = catalog.next_pointer(state.topic_id if state else None)
    if pointer is None:
        return None
    values = dict(
        offer_id=uuid.uuid4(), offer_sequence=state.offer_sequence + 1 if state else 1,
        topic_id=pointer.topic_id, topic_revision=pointer.topic_revision,
        questions=catalog.questions(pointer).model_dump(),
        day_high_watermark=max(state.day_high_watermark, day.local_date) if state else day.local_date,
        completed=False, offered_at=day.served_at, updated_at=day.served_at,
    )
    if state is None:
        state = UserTopicState(user_id=user_id, placement=PLACEMENT, **values)
        session.add(state)
    else:
        for key, value in values.items():
            setattr(state, key, value)
    await session.flush()
    return state


async def supersede_pending(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Caller holds the shared user lock; invoked on every successful chat turn."""
    await session.execute(update(ChatTopicEntry).where(
        ChatTopicEntry.user_id == user_id, ChatTopicEntry.state == "pending",
    ).values(state="superseded"))


async def prepare_entry(
    session: AsyncSession, user_id: uuid.UUID, offer_id: uuid.UUID,
    catalog: TopicCatalog, now: datetime, timezone_name: str, context_revision: int,
) -> ChatTopicEntry:
    """Ensure one opening for the explicit offer, without running a model.

    Endpoint idempotency must be checked before calling this function. A retry
    restores the original entry even when its state is now terminal.
    """
    await advisory_xact_lock(session, user_id)
    state = await session.get(UserTopicState, (user_id, PLACEMENT), populate_existing=True)
    if (state is None or state.offer_id != offer_id or state.completed
            or not catalog.manifest.enabled or state.topic_id not in catalog.positions
            or catalog.is_revoked(state.topic_id, state.topic_revision)):
        raise AppError("TOPIC_OFFER_UNAVAILABLE", 409, "새 대화 주제를 확인해 주세요.")
    pending = await session.scalar(select(ChatTopicEntry).where(
        ChatTopicEntry.user_id == user_id, ChatTopicEntry.state == "pending",
    ).execution_options(populate_existing=True))
    if (pending is not None and pending.offer_id == offer_id
            and pending.context_revision == context_revision and now < pending.expires_at):
        return pending
    if AppDay.at(now, timezone_name).local_date > state.day_high_watermark:
        raise AppError("TOPIC_OFFER_UNAVAILABLE", 409, "새 대화 주제를 확인해 주세요.")
    await supersede_pending(session, user_id)
    entry = ChatTopicEntry(
        id=uuid.uuid4(), user_id=user_id, placement=PLACEMENT,
        offer_id=offer_id, offer_sequence=state.offer_sequence,
        topic_id=state.topic_id, topic_revision=state.topic_revision,
        questions=dict(state.questions), context_revision=context_revision, state="pending",
        timezone_name=timezone_name, local_date=AppDay.at(now, timezone_name).local_date,
        created_at=now, expires_at=entry_expiration(now, timezone_name),
    )
    session.add(entry)
    await session.flush()
    return entry


async def admit_entry(
    session: AsyncSession, user_id: uuid.UUID, entry_id: uuid.UUID,
    catalog: TopicCatalog, now: datetime, context_revision: int,
) -> ChatTopicEntry:
    """Caller holds the chat lease and user lock. Check expiry only at admission."""
    await advisory_xact_lock(session, user_id)
    entry = await session.scalar(select(ChatTopicEntry).where(
        ChatTopicEntry.id == entry_id, ChatTopicEntry.user_id == user_id,
    ).execution_options(populate_existing=True))
    if (entry is None or entry.state != "pending" or entry.expires_at <= now
            or entry.context_revision != context_revision or not catalog.manifest.enabled
            or catalog.is_revoked(entry.topic_id, entry.topic_revision)):
        raise AppError("TOPIC_ENTRY_UNAVAILABLE", 409, "대화 주제를 다시 열어 주세요.")
    return entry


async def complete_entry(
    session: AsyncSession, user_id: uuid.UUID, entry_id: uuid.UUID,
    catalog: TopicCatalog, context_revision: int, first_user_message_id: int,
    greeting_message_id: int | None, now: datetime,
) -> None:
    """Publish under the verified chat lease, in the same transaction as messages.

    Expiry passing during model execution is allowed. Supersession, revocation,
    and context changes are not. A safety response can omit the opening.
    """
    await advisory_xact_lock(session, user_id)
    entry = await session.scalar(select(ChatTopicEntry).where(
        ChatTopicEntry.id == entry_id, ChatTopicEntry.user_id == user_id,
    ).execution_options(populate_existing=True))
    if (entry is None or entry.state != "pending"
            or entry.context_revision != context_revision or not catalog.manifest.enabled
            or catalog.is_revoked(entry.topic_id, entry.topic_revision)):
        raise AppError("TOPIC_ENTRY_UNAVAILABLE", 409, "대화 주제를 다시 열어 주세요.")
    entry.first_user_message_id = first_user_message_id
    entry.committed_message_id = greeting_message_id
    entry.state = "committed" if greeting_message_id is not None else "superseded"
    await session.execute(update(UserTopicState).where(
        UserTopicState.user_id == user_id, UserTopicState.placement == PLACEMENT,
        UserTopicState.offer_id == entry.offer_id,
    ).values(completed=True, updated_at=now))
    await session.flush()
