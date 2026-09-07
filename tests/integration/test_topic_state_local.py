"""Real PostgreSQL serialization tests, restricted to a disposable local schema."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.app_day import AppDay
from app.core.db import Base
from app.core.errors import AppError
from app.models.message import Message
from app.models.profile import Profile
from app.models.topic import ChatTopicEntry, UserTopicState
from app.services.topic_catalog import TopicCatalog
from app.services.topic_state import admit_entry, complete_entry, prepare_entry, resolve_offer

NOW = datetime(2026, 9, 7, 14, 59, tzinfo=timezone.utc)
TZ = "Asia/Seoul"


@pytest.fixture
async def database():
    dsn = os.environ.get("TOPIC_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TOPIC_TEST_DATABASE_URL required")
    if urlsplit(dsn).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("local PostgreSQL only")
    schema = "topic_test_" + uuid.uuid4().hex
    engine = create_async_engine(dsn, execution_options={"schema_translate_map": {None: schema}})
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
            Profile.__table__, Message.__table__, UserTopicState.__table__, ChatTopicEntry.__table__,
        ]))
    uid = uuid.uuid4()
    try:
        async with AsyncSession(engine) as session:
            session.add(Profile(id=uid))
            await session.commit()
        yield engine, uid, TopicCatalog.load()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def resolve(database, now=NOW):
    engine, uid, catalog = database
    async with AsyncSession(engine, expire_on_commit=False) as session:
        offer = await resolve_offer(session, uid, catalog, AppDay.at(now, TZ))
        await session.commit()
        return offer


async def test_concurrent_resolves_allocate_once_and_absent_days_advance_once(database):
    offers = await asyncio.gather(*(resolve(database) for _ in range(12)))
    assert len({o.offer_id for o in offers}) == 1
    next_offers = await asyncio.gather(*(
        resolve(database, NOW + timedelta(days=8)) for _ in range(12)
    ))
    assert len({o.offer_id for o in next_offers}) == 1
    assert {o.offer_sequence for o in next_offers} == {2}


async def test_concurrent_prepare_restores_same_entry(database):
    offer = await resolve(database)
    engine, uid, catalog = database

    async def prepare():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            entry = await prepare_entry(session, uid, offer.offer_id, catalog, NOW, TZ, 0)
            await session.commit()
            return entry.id

    assert len(set(await asyncio.gather(*(prepare() for _ in range(8))))) == 1


async def test_old_prepared_answer_does_not_complete_new_offer(database):
    offer = await resolve(database)
    engine, uid, catalog = database
    async with AsyncSession(engine, expire_on_commit=False) as session:
        entry = await prepare_entry(session, uid, offer.offer_id, catalog, NOW, TZ, 0)
        entry_id = entry.id
        await session.commit()
        new_offer = await resolve(database, NOW + timedelta(minutes=2))
        greeting = Message(user_id=uid, sender="moly", content="question", activity_date=NOW.date())
        answer = Message(user_id=uid, sender="user", content="answer", activity_date=NOW.date())
        session.add_all([greeting, answer])
        await session.flush()
        await complete_entry(session, uid, entry_id, catalog, 0, answer.id, greeting.id,
                             NOW + timedelta(minutes=2))
        await session.commit()
    current = await resolve(database, NOW + timedelta(minutes=3))
    assert current.offer_id == new_offer.offer_id
    assert current.completed is False


async def test_completed_and_midnight_advance_only_once(database):
    offer = await resolve(database)
    engine, uid, _ = database
    async with AsyncSession(engine) as session:
        row = await session.get(UserTopicState, (uid, "home_blind"))
        row.completed = True
        await session.commit()
    next_offer = await resolve(database, NOW + timedelta(days=3))
    assert next_offer.offer_sequence == offer.offer_sequence + 1


async def test_expired_old_offer_cannot_be_reprepared_after_midnight(database):
    offer = await resolve(database)
    engine, uid, catalog = database
    async with AsyncSession(engine) as session:
        with pytest.raises(AppError):
            await prepare_entry(session, uid, offer.offer_id, catalog,
                                NOW + timedelta(hours=1), TZ, 0)


async def test_foreign_user_cannot_prepare_offer(database):
    offer = await resolve(database)
    engine, _, catalog = database
    async with AsyncSession(engine) as session:
        with pytest.raises(AppError):
            await prepare_entry(session, uuid.uuid4(), offer.offer_id, catalog, NOW, TZ, 0)


async def test_publish_failure_rolls_back_messages_and_completion(database):
    offer = await resolve(database)
    engine, uid, catalog = database
    async with AsyncSession(engine, expire_on_commit=False) as session:
        entry = await prepare_entry(session, uid, offer.offer_id, catalog, NOW, TZ, 0)
        entry_id = entry.id
        await session.commit()
        answer = Message(user_id=uid, sender="user", content="answer", activity_date=NOW.date())
        session.add(answer)
        await session.flush()
        # A nonexistent greeting proves that the FK fails the entire publication.
        with pytest.raises(IntegrityError):
            await complete_entry(session, uid, entry_id, catalog, 0, answer.id, 999999, NOW)
        await session.rollback()
        assert await session.scalar(select(Message.id)) is None
        state = await session.get(UserTopicState, (uid, "home_blind"))
        assert state.completed is False
        restored = await session.get(ChatTopicEntry, entry_id)
        assert restored.state == "pending"


async def test_admitted_turn_can_finish_after_expiry_but_new_admission_cannot(database):
    offer = await resolve(database)
    engine, uid, catalog = database
    async with AsyncSession(engine, expire_on_commit=False) as session:
        entry = await prepare_entry(session, uid, offer.offer_id, catalog, NOW, TZ, 0)
        entry_id = entry.id
        await session.commit()
        await admit_entry(session, uid, entry_id, catalog, NOW, 0)
        await session.commit()
        later = NOW + timedelta(hours=1)
        with pytest.raises(AppError):
            await admit_entry(session, uid, entry_id, catalog, later, 0)
        await session.rollback()
        answer = Message(user_id=uid, sender="user", content="answer", activity_date=NOW.date())
        session.add(answer)
        await session.flush()
        # Safety response omits the opening but still establishes an answered offer.
        await complete_entry(session, uid, entry_id, catalog, 0, answer.id, None, later)
        await session.commit()
        state = await session.get(UserTopicState, (uid, "home_blind"))
        assert state.completed
        assert entry.first_user_message_id == answer.id
        assert entry.state == "superseded"
