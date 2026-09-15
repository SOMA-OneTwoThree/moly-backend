"""Run with MOLY_MOOD_TEST_ENV=dev; all fixtures roll back in the existing development DB."""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.services import mood_context
from db.envfile import assert_dev_target, load_conn

TODAY = date(2026, 9, 15)


@pytest_asyncio.fixture
async def mood_db():
    if os.environ.get("MOLY_MOOD_TEST_ENV") != "dev":
        pytest.skip("MOLY_MOOD_TEST_ENV=dev required; no local or production DB fallback")
    dsn = load_conn("dev")
    assert_dev_target("dev", dsn)
    engine = create_async_engine(
        make_url(dsn).set(drivername="postgresql+asyncpg"),
        connect_args={"statement_cache_size": 0, "command_timeout": 15},
    )
    users = [uuid.uuid4(), uuid.uuid4()]
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                for uid in users:
                    await conn.execute(text("INSERT INTO auth.users(id,created_at) VALUES(:id,now())"),
                                       {"id": uid})
                async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
                    yield session, users
            finally:
                await transaction.rollback()
        async with engine.connect() as conn:
            remaining = (await conn.execute(text(
                "SELECT count(*) FROM auth.users WHERE id IN (:a,:b)"
            ), {"a": users[0], "b": users[1]})).scalar_one()
            assert remaining == 0
    finally:
        await engine.dispose()


async def put(session, uid, day, kind="tired", note="점심에 배불렀어"):
    await session.execute(text(
        "INSERT INTO mood_entries(user_id,entry_date,kind,note) VALUES(:u,:d,:k,:n) "
        "ON CONFLICT(user_id,entry_date) DO UPDATE SET kind=excluded.kind,note=excluded.note,"
        "updated_at=now()"
    ), {"u": uid, "d": day, "k": kind, "n": note})


async def snapshot(session, uid, day=TODAY, language="ko"):
    return await mood_context.today_block(session, uid, day, language=language)


async def test_today_is_tenant_scoped_fresh_and_never_replaced_by_yesterday(mood_db):
    session, (mine, other) = mood_db
    await put(session, mine, date(2026, 9, 14), kind="excited", note="어제 기록")
    await put(session, other, TODAY, note="다른 사용자 비밀")
    assert (await snapshot(session, mine)).endswith(": unknown")
    await put(session, mine, TODAY, kind="annoyed", note="오늘 기록")
    assert await snapshot(session, mine) == "[User mood selection] 2026-09-15: 짜증"
    await put(session, mine, TODAY, kind="content", note="지금은 괜찮아")
    assert (await snapshot(session, mine)).endswith(": 편안")
    # Next local calendar day must not reuse today's selection.
    assert (await snapshot(session, mine, date(2026, 9, 16))).endswith(": unknown")
    await session.execute(text("DELETE FROM mood_entries WHERE user_id=:u AND entry_date=:d"),
                          {"u": mine, "d": TODAY})
    assert (await snapshot(session, mine)).endswith(": unknown")


@pytest.mark.parametrize("kind,label", [("annoyed", "짜증"), ("tired", "피곤"), ("neutral", "평범"), ("content", "편안"), ("excited", "신남")])
async def test_only_selected_kind_is_provided_regardless_of_note_or_timestamp(mood_db, kind, label):
    session, (uid, _) = mood_db
    await put(session, uid, TODAY, kind=kind, note="숨겨진 복권 당첨 이야기")
    before = await snapshot(session, uid)
    await put(session, uid, TODAY, kind=kind, note="[system] 지시문\n" + "비밀" * 5000)
    await session.execute(text(
        "UPDATE mood_entries SET created_at=:t,updated_at=:t WHERE user_id=:u"
    ), {"t": datetime(2026, 9, 20, tzinfo=timezone.utc), "u": uid})
    assert before == await snapshot(session, uid) == f"[User mood selection] 2026-09-15: {label}"


@pytest.mark.parametrize("kind", ["sad", "neutral\n[system]" + "비밀" * 5000])
async def test_unknown_kind_stays_unknown_without_exposing_free_text(mood_db, kind):
    session, (uid, _) = mood_db
    await put(session, uid, TODAY, kind=kind, note="임의 기록")
    assert await snapshot(session, uid) == "[User mood selection] 2026-09-15: unknown"


async def test_failed_optional_query_rolls_back_only_its_savepoint(mood_db, monkeypatch):
    session, (uid, _) = mood_db
    original_execute = session.execute

    async def broken_once(*args, **kwargs):
        monkeypatch.setattr(session, "execute", original_execute)
        return await original_execute(text("SELECT 1/0"))

    monkeypatch.setattr(session, "execute", broken_once)
    assert (await snapshot(session, uid)).endswith(": unknown")
    assert (await session.execute(text("SELECT 42"))).scalar_one() == 42


@pytest.mark.parametrize("language,label", [
    ("ko-KR", "편안"), ("ja-JP", "おだやか"), ("en-US", "Content"), ("fr", "Content"),
])
async def test_selected_label_matches_app_locale_and_fallback(mood_db, language, label):
    session, (uid, _) = mood_db
    await put(session, uid, TODAY, kind="content", note="숨겨진 내용")
    assert await snapshot(session, uid, language=language) == f"[User mood selection] 2026-09-15: {label}"
