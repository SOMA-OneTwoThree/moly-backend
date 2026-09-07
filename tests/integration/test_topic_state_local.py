"""Real PostgreSQL serialization tests, restricted to a disposable local schema."""

import asyncio
import os
import uuid
from pathlib import Path
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
from app.models.chat_context import ChatContext
from app.models.conversational_recall import ChatActiveTurn
from app.models.idempotency_key import IdempotencyKey
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
    engine = create_async_engine(dsn, execution_options={"schema_translate_map": {None: schema}},
                                 connect_args={"server_settings": {"search_path": schema}})
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
            Profile.__table__, Message.__table__, UserTopicState.__table__, ChatTopicEntry.__table__,
            ChatContext.__table__, ChatActiveTurn.__table__, IdempotencyKey.__table__,
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


async def test_prepare_idempotency_restores_terminal_state_and_rejects_changed_body(database):
    from app.schemas.topics import PrepareTopicRequest, TopicReference
    from app.services.topic_entries import prepare
    from app.services.banner_catalog import BannerCatalog, capabilities
    offer = await resolve(database)
    engine, uid, catalog = database
    banners = BannerCatalog.load()
    banner = next(b for b in banners.manifest.banners if any(
        binding.source == "topic.question" for binding in b.bindings.values()
    ))
    request = PrepareTopicRequest(placement="home_blind", schema_version=1,
        platform="ios", app_version="1.0.0", capabilities=list(capabilities(banner.canvases_by_locale['ko'])),
        banner_id=banner.id, topic_ref=TopicReference(offer_id=offer.offer_id,
        offer_sequence=offer.offer_sequence, topic_id=offer.topic_id,
        topic_revision=offer.topic_revision, locale="ko"))

    async def run(req=request):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await prepare(session, str(uid), req, "retry-key", banner_catalog=banners,
                topic_catalog=catalog, now=NOW, timezone_name=TZ)
            await session.commit()
            return result

    results = await asyncio.gather(*(run() for _ in range(6)))
    assert len({result.entry_id for result in results}) == 1
    async with AsyncSession(engine) as session:
        entry = await session.get(ChatTopicEntry, results[0].entry_id)
        entry.state = "superseded"
        await session.commit()
    replay = await run()
    assert replay.state == "superseded"
    assert not hasattr(replay, "content")
    with pytest.raises(AppError) as failure:
        await run(request.model_copy(update={"platform": "android"}))
    assert failure.value.code == "IDEMPOTENCY_CONFLICT"


async def test_migration_is_reentrant_and_denies_client_roles(database):
    import asyncpg
    dsn = os.environ["TOPIC_TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    transaction = conn.transaction()
    await transaction.start()
    suffix = uuid.uuid4().hex
    schema, anon, authenticated = (f"migration_{suffix}", f"anon_{suffix}", f"auth_{suffix}")
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"; CREATE ROLE "{anon}"; CREATE ROLE "{authenticated}";')
        await conn.execute(f'CREATE TABLE "{schema}".profiles (id uuid PRIMARY KEY); '
            f'CREATE TABLE "{schema}".messages (id bigint PRIMARY KEY, user_id uuid, '
            "kind text NOT NULL DEFAULT 'normal' CONSTRAINT messages_kind_check "
            "CHECK(kind IN ('normal','greeting','fortune_context_root','fortune_derived')), "
            'UNIQUE(user_id,id));')
        for _ in range(2):
            for stage in ('prepare', 'validate', 'swap'):
                kind_sql = Path(f"db/migrations/20260907_topic_kind_constraint_{stage}.sql").read_text()
                # Keep the DO block's BEGIN; only remove the outer transaction commands.
                kind_sql = kind_sql.replace("BEGIN;", "", 1).replace("COMMIT;", "")
                kind_sql = kind_sql.replace("public.", f'"{schema}".')
                await conn.execute(kind_sql)
        await conn.execute(f"INSERT INTO \"{schema}\".messages(id,kind) VALUES (1,'topic_opening')")
        sql = Path("db/migrations/20260907_banner_topic_conversation.sql").read_text()
        sql = sql.replace("BEGIN;", "").replace("COMMIT;", "").replace("public.", f'"{schema}".')
        sql = sql.replace("FROM anon, authenticated", f'FROM "{anon}", "{authenticated}"')
        await conn.execute(sql)
        await conn.execute(sql)
        for table, model in [("user_topic_states", UserTopicState), ("chat_topic_entries", ChatTopicEntry)]:
            columns = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema=$1 AND table_name=$2", schema, table)
            assert {row['column_name'] for row in columns} == set(model.__table__.columns.keys())
            assert await conn.fetchval("SELECT relrowsecurity FROM pg_class WHERE oid=$1::regclass", f'{schema}.{table}')
            for role in (anon, authenticated):
                assert not await conn.fetchval("SELECT has_table_privilege($1,$2,'SELECT,INSERT,UPDATE,DELETE')", role, f'{schema}.{table}')
    finally:
        await transaction.rollback()
        await conn.close()
