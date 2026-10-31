"""Daily practice on a disposable local DB: real constraints, advisory lock and cascade.

Concurrency needs real commits on separate connections, so rows are committed and removed afterwards.
"""
from __future__ import annotations

import asyncio
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.errors import AppError
from app.services import daily_practice, privacy
from db.schema_contract import require_scratch

ENSURE_ACTIVE = privacy.ensure_subject_active
SEOUL = "Asia/Seoul"
NOW = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)  # 12:00 in Seoul
SEED_SQL = Path(__file__).parents[2] / "db/changes/daily_practice_seed.sql"
PREFIX = "로컬 테스트"


@pytest_asyncio.fixture
async def engine():
    dsn = os.environ.get("MOLY_SCHEMA_TEST_DSN")
    if not dsn:
        pytest.skip("MOLY_SCHEMA_TEST_DSN required")
    require_scratch(dsn)
    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://"),
                                 poolclass=NullPool, connect_args={"statement_cache_size": 0})
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text(f"DELETE FROM daily_practice_offers WHERE action_id IN "
                                f"(SELECT id FROM daily_practice_actions WHERE title_ko LIKE '{PREFIX}%')"))
        await conn.execute(text(f"DELETE FROM daily_practice_actions WHERE title_ko LIKE '{PREFIX}%'"))
    await engine.dispose()


async def signup(engine) -> str:
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO auth.users(id) VALUES (:id)"), {"id": uid})
    return str(uid)


async def add_actions(engine, count: int, category: str = "rest") -> None:
    async with engine.begin() as conn:
        for i in range(count):
            await conn.execute(text("""
                INSERT INTO daily_practice_actions
                  (category, slots, icon, title_ko, title_en, title_ja, speech_ko, speech_en, speech_ja,
                   source, prompt_version, model)
                VALUES (:category, ARRAY['morning','day','evening','night']::text[], 'droplet', :ko, 'E', 'J',
                        '말', 'Say', 'ね', 'seed', 'v', 'm')
            """), {"category": category, "ko": f"{PREFIX} {uuid.uuid4().hex[:6]} {i}"})


async def call(engine, function, *args, **kwargs):
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await function(session, *args, **kwargs)


def common(**extra):
    return {"locale": "ko", "timezone_name": SEOUL, "now_utc": NOW} | extra


async def scalar(engine, sql, **params):
    async with engine.connect() as conn:
        return await conn.scalar(text(sql), params)


async def test_full_flow_through_a_routine(engine):
    await add_actions(engine, 5)
    user = await signup(engine)
    first = await call(engine, daily_practice.offer, user, focus=["rest"], **common())
    assert first["practice"]["status"] == "offered" and first["offers_remaining"] == 2
    assert first["local_date"].isoformat() == "2026-11-01"
    assert first["expires_at"] == datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
    second = await call(engine, daily_practice.skip, user, str(first["practice"]["id"]), focus=[], **common())
    assert second["offers_remaining"] == 1
    practice_id = str(second["practice"]["id"])
    assert (await call(engine, daily_practice.accept, user, practice_id, **common()))["practice"]["status"] == "accepted"
    assert (await call(engine, daily_practice.complete, user, practice_id, **common()))["practice"]["status"] == "completed"

    added = await call(engine, daily_practice.add_routine, user, practice_id, **common())
    again = await call(engine, daily_practice.add_routine, user, practice_id, **common())
    assert again["routine"]["id"] == added["routine"]["id"]
    assert added["today"]["practice"]["routine_added"] is True
    assert await scalar(engine, "SELECT count(*) FROM routines WHERE user_id = :u AND name_i18n IS NOT NULL",
                        u=uuid.UUID(user)) == 1
    assert await scalar(engine, "SELECT count(*) FROM routine_schedules WHERE user_id = :u", u=uuid.UUID(user)) == 1
    with pytest.raises(AppError) as blocked:
        await call(engine, daily_practice.uncomplete, user, practice_id, **common())
    assert blocked.value.code == "DAILY_PRACTICE_INVALID_STATE"


async def test_concurrent_offers_make_one_row(engine):
    await add_actions(engine, 10)
    user = await signup(engine)
    results = await asyncio.gather(*[
        call(engine, daily_practice.offer, user, focus=[], rng=random.Random(i), **common())
        for i in range(8)
    ])
    assert len({result["practice"]["id"] for result in results}) == 1
    assert await scalar(engine, "SELECT count(*) FROM daily_practice_offers WHERE user_id = :u",
                        u=uuid.UUID(user)) == 1


async def test_concurrent_skips_and_accepts_never_leave_two_live_rows(engine):
    await add_actions(engine, 10)
    user = await signup(engine)
    first = await call(engine, daily_practice.offer, user, focus=[], **common())
    practice_id = str(first["practice"]["id"])
    outcomes = await asyncio.gather(
        *[call(engine, daily_practice.skip, user, practice_id, focus=[], **common()) for _ in range(4)],
        *[call(engine, daily_practice.accept, user, practice_id, **common()) for _ in range(4)],
        return_exceptions=True,
    )
    assert all(isinstance(o, (dict, AppError)) for o in outcomes), outcomes
    assert await scalar(engine, """
        SELECT count(*) FROM daily_practice_offers
        WHERE user_id = :u AND local_date = '2026-11-01' AND status <> 'skipped'
    """, u=uuid.UUID(user)) == 1


async def test_concurrent_routine_requests_make_one_routine(engine):
    await add_actions(engine, 3)
    user = await signup(engine)
    practice_id = str((await call(engine, daily_practice.offer, user, focus=[], **common()))["practice"]["id"])
    await call(engine, daily_practice.accept, user, practice_id, **common())
    await call(engine, daily_practice.complete, user, practice_id, **common())
    results = await asyncio.gather(*[
        call(engine, daily_practice.add_routine, user, practice_id, **common()) for _ in range(5)
    ])
    assert len({result["routine"]["id"] for result in results}) == 1
    assert await scalar(engine, "SELECT count(*) FROM routines WHERE user_id = :u AND template_id IS NULL",
                        u=uuid.UUID(user)) == 1


async def test_database_refuses_a_second_live_row_for_a_local_date(engine):
    await add_actions(engine, 2)
    user = await signup(engine)
    await call(engine, daily_practice.offer, user, focus=[], **common())
    other = await scalar(engine, """
        SELECT id FROM daily_practice_actions
        WHERE id NOT IN (SELECT action_id FROM daily_practice_offers WHERE user_id = :u)
        LIMIT 1
    """, u=uuid.UUID(user))
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO daily_practice_offers(user_id, action_id, local_date, expires_at, status)
                VALUES (:u, :a, '2026-11-01', now(), 'accepted')
            """), {"u": uuid.UUID(user), "a": other})


async def test_expired_row_stays_as_history_and_a_new_day_offers_again(engine):
    await add_actions(engine, 4)
    user = await signup(engine)
    first = await call(engine, daily_practice.offer, user, focus=[], **common())
    await call(engine, daily_practice.accept, user, str(first["practice"]["id"]), **common())
    tomorrow = NOW + timedelta(days=1)
    with pytest.raises(AppError) as expired:
        await call(engine, daily_practice.complete, user, str(first["practice"]["id"]),
                   **common(now_utc=tomorrow))
    assert expired.value.code == "DAILY_PRACTICE_EXPIRED"
    next_day = await call(engine, daily_practice.offer, user, focus=[], **common(now_utc=tomorrow))
    assert next_day["practice"]["id"] != first["practice"]["id"]
    assert next_day["practice"]["title"] != first["practice"]["title"]


async def test_recent_mood_rows_are_read_with_real_columns(engine):
    await add_actions(engine, 3, "calm")
    await add_actions(engine, 3, "body")
    user = await signup(engine)
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO mood_entries(user_id, entry_date, kind) VALUES (:u, '2026-11-01', 'tired')"
        ), {"u": uuid.UUID(user)})
    body = await call(engine, daily_practice.offer, user, focus=[], rng=random.Random(1), **common())
    assert body["practice"]["category"] in {"calm", "rest", "self_kindness"}


async def test_top_up_enqueues_a_real_job_once_per_category_and_day(engine):
    user = await signup(engine)
    await add_actions(engine, 1, "mind")
    first = await call(engine, daily_practice.offer, user, focus=["mind"], **common())
    await call(engine, daily_practice.skip, user, str(first["practice"]["id"]), focus=["mind"], **common())
    jobs_for_key = await scalar(engine, """
        SELECT count(*) FROM async_jobs
        WHERE job_type = 'daily_practice_top_up' AND dedup_key = 'daily_practice_top_up:mind:2026-11-01'
          AND user_id IS NULL
    """)
    assert jobs_for_key == 1
    await call(engine, daily_practice.offer, user, focus=["mind"], **common())
    assert await scalar(engine, """
        SELECT count(*) FROM async_jobs WHERE dedup_key = 'daily_practice_top_up:mind:2026-11-01'
    """) == 1


async def test_account_deletion_removes_rows_and_leaves_the_pool(engine, monkeypatch):
    monkeypatch.setattr(privacy, "ensure_subject_active", ENSURE_ACTIVE)
    await add_actions(engine, 3)
    pool_before = await scalar(engine, "SELECT count(*) FROM daily_practice_actions")
    user = await signup(engine)
    uid = uuid.UUID(user)
    first = await call(engine, daily_practice.offer, user, focus=[], **common())
    practice_id = str(first["practice"]["id"])
    await call(engine, daily_practice.accept, user, practice_id, **common())
    await call(engine, daily_practice.complete, user, practice_id, **common())
    await call(engine, daily_practice.add_routine, user, practice_id, **common())

    async with engine.begin() as conn:
        await conn.execute(text("SELECT public.begin_subject_deletion(:u, :o)"), {"u": uid, "o": uuid.uuid4()})
    for function, args in [
        (daily_practice.status, ()),
        (daily_practice.offer, ()),
        (daily_practice.accept, (practice_id,)),
    ]:
        extra = {"focus": []} if function is daily_practice.offer else {}
        with pytest.raises(AppError) as blocked:
            await call(engine, function, user, *args, **common(**extra))
        assert blocked.value.code == "ACCOUNT_DELETING"

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM auth.users WHERE id = :u"), {"u": uid})
    assert await scalar(engine, "SELECT count(*) FROM daily_practice_offers WHERE user_id = :u", u=uid) == 0
    assert await scalar(engine, "SELECT count(*) FROM daily_practice_actions") == pool_before


async def test_seed_sql_is_valid_and_idempotent(engine):
    if not SEED_SQL.exists():
        pytest.skip("시드 SQL이 아직 없다: scripts/daily_practice_seed.py sql")
    statement = SEED_SQL.read_text(encoding="utf-8")
    insert = statement[statement.index("INSERT INTO"):]
    async with AsyncSession(engine) as session:
        await session.execute(text(insert))
        first = await session.scalar(text("SELECT count(*) FROM daily_practice_actions WHERE source = 'seed'"))
        await session.execute(text(insert))
        second = await session.scalar(text("SELECT count(*) FROM daily_practice_actions WHERE source = 'seed'"))
        await session.rollback()
    assert first == second > 0
