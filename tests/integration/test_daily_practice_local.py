"""Daily practice on a disposable local DB: real constraints, advisory lock and cascade.

Concurrency needs real commits on separate connections, so rows are committed and removed afterwards.
"""
from __future__ import annotations

import asyncio
import os
import random
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.errors import AppError
from app.services import daily_practice
from db.schema_contract import require_scratch

SEOUL = "Asia/Seoul"
NOW = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)  # 12:00 in Seoul


@pytest_asyncio.fixture
async def engine():
    dsn = os.environ.get("MOLY_SCHEMA_TEST_DSN")
    if not dsn:
        pytest.skip("MOLY_SCHEMA_TEST_DSN required")
    require_scratch(dsn)
    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://"),
                                 poolclass=NullPool, connect_args={"statement_cache_size": 0})
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM daily_practice_offers"))
        await conn.execute(text("DELETE FROM daily_practice_templates"))
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM daily_practice_offers"))
        await conn.execute(text("DELETE FROM daily_practice_templates"))
    await engine.dispose()


async def signup(engine) -> str:
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO auth.users(id) VALUES (:id)"), {"id": uid})
    return str(uid)


async def add_templates(engine, count: int) -> None:
    async with engine.begin() as conn:
        for i in range(count):
            await conn.execute(text("""
                INSERT INTO daily_practice_templates (title_ko, title_en, title_ja)
                VALUES (:ko, :en, :ja)
            """), {"ko": f"행동 {i}", "en": f"Action {i}", "ja": f"行動{i}"})


async def call(engine, function, *args, **kwargs):
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await function(session, *args, **kwargs)


def common(**extra):
    return {"locale": "ko", "timezone_name": SEOUL, "now_utc": NOW} | extra


async def scalar(engine, sql, **params):
    async with engine.connect() as conn:
        return await conn.scalar(text(sql), params)


async def completed(engine, user, **extra):
    practice_id = str((await call(engine, daily_practice.offer, user, **common(**extra)))["options"][0]["id"])
    await call(engine, daily_practice.accept, user, practice_id, **common(**extra))
    await call(engine, daily_practice.complete, user, practice_id, **common(**extra))
    return practice_id


async def test_full_flow_through_a_routine(engine):
    await add_templates(engine, 5)
    user = await signup(engine)
    uid = uuid.UUID(user)
    assert (await call(engine, daily_practice.status, user, **common()))["options"] == []
    first = await call(engine, daily_practice.offer, user, **common())
    assert first["can_choose"] is True and len(first["options"]) == 3
    assert first["local_date"] == date(2026, 11, 1)
    assert first["expires_at"] == datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
    assert (await call(engine, daily_practice.offer, user, **common()))["options"] == first["options"]

    practice_id = str(first["options"][1]["id"])
    chosen = await call(engine, daily_practice.accept, user, practice_id, **common())
    assert chosen["practice"]["deadline"] == NOW + timedelta(hours=24)
    assert chosen["expires_at"] == NOW + timedelta(hours=24) and chosen["options"] == []
    done = await call(engine, daily_practice.complete, user, practice_id, **common())
    assert done["practice"]["status"] == "completed" and done["can_choose"] is False
    assert await scalar(engine, "SELECT completed_local_date FROM daily_practice_offers WHERE id = :i",
                        i=uuid.UUID(practice_id)) == date(2026, 11, 1)

    added = await call(engine, daily_practice.add_routine, user, practice_id, **common())
    again = await call(engine, daily_practice.add_routine, user, practice_id, **common())
    assert again["routine"]["id"] == added["routine"]["id"]
    assert added["routine"]["icon"] == "seedling" and added["routine"]["color"] == "peach"
    assert added["today"]["practice"]["routine_added"] is True
    assert await scalar(engine, "SELECT count(*) FROM routines WHERE user_id = :u AND name_i18n ? 'ja'", u=uid) == 1
    assert await scalar(engine, "SELECT count(*) FROM routine_schedules WHERE user_id = :u", u=uid) == 1
    with pytest.raises(AppError) as blocked:
        await call(engine, daily_practice.uncomplete, user, practice_id, **common())
    assert blocked.value.code == "DAILY_PRACTICE_INVALID_STATE"


async def test_concurrent_offers_make_one_set(engine):
    await add_templates(engine, 10)
    user = await signup(engine)
    results = await asyncio.gather(*[
        call(engine, daily_practice.offer, user, rng=random.Random(i), **common()) for i in range(8)
    ])
    assert len({tuple(o["id"] for o in result["options"]) for result in results}) == 1
    assert await scalar(engine, "SELECT count(*) FROM daily_practice_offers WHERE user_id = :u",
                        u=uuid.UUID(user)) == 3


async def test_concurrent_accepts_allow_one_choice_per_local_date(engine):
    await add_templates(engine, 10)
    user = await signup(engine)
    options = (await call(engine, daily_practice.offer, user, **common()))["options"]
    outcomes = await asyncio.gather(*[
        call(engine, daily_practice.accept, user, str(option["id"]), **common()) for option in options
    ], return_exceptions=True)
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    assert {o.code for o in outcomes if not isinstance(o, dict)} == {"DAILY_PRACTICE_INVALID_STATE"}
    assert await scalar(engine, """
        SELECT count(*) FROM daily_practice_offers WHERE user_id = :u AND status <> 'offered'
    """, u=uuid.UUID(user)) == 1


async def test_concurrent_routine_requests_make_one_routine(engine):
    await add_templates(engine, 3)
    user = await signup(engine)
    practice_id = await completed(engine, user)
    results = await asyncio.gather(*[
        call(engine, daily_practice.add_routine, user, practice_id, **common()) for _ in range(5)
    ])
    assert len({result["routine"]["id"] for result in results}) == 1
    assert await scalar(engine, "SELECT count(*) FROM routines WHERE user_id = :u AND template_id IS NULL",
                        u=uuid.UUID(user)) == 1


async def test_database_refuses_a_second_choice_for_a_local_date(engine):
    await add_templates(engine, 2)
    user = await signup(engine)
    options = (await call(engine, daily_practice.offer, user, **common()))["options"]
    await call(engine, daily_practice.accept, user, str(options[0]["id"]), **common())
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text("""
                UPDATE daily_practice_offers SET status = 'accepted', deadline = now() WHERE id = :i
            """), {"i": options[1]["id"]})


async def test_database_refuses_the_same_template_twice_on_a_local_date(engine):
    await add_templates(engine, 1)
    user = await signup(engine)
    await call(engine, daily_practice.offer, user, **common())
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO daily_practice_offers (user_id, template_id, local_date, options_expire_at)
                SELECT user_id, template_id, local_date, options_expire_at FROM daily_practice_offers
                WHERE user_id = :u
            """), {"u": uuid.UUID(user)})


@pytest.mark.parametrize("title", ["", "가" * 61])
async def test_database_refuses_titles_outside_the_length_limit(engine, title):
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO daily_practice_templates (title_ko, title_en, title_ja) VALUES (:t, 'E', 'J')
            """), {"t": title})


async def test_database_refuses_a_duplicate_korean_title(engine):
    await add_templates(engine, 1)
    with pytest.raises(IntegrityError):
        await add_templates(engine, 1)


async def test_rls_is_on_and_only_service_role_has_grants(engine):
    for table in ("daily_practice_templates", "daily_practice_offers"):
        assert await scalar(engine, "SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:t)",
                            t=f"public.{table}") is True
        for role, allowed in (("service_role", True), ("anon", False), ("authenticated", False)):
            assert await scalar(engine, "SELECT has_table_privilege(CAST(:r AS name), CAST(:t AS text), 'SELECT')",
                                r=role, t=f"public.{table}") is allowed


async def test_30_day_avoidance_and_least_recent_fallback(engine):
    await add_templates(engine, 4)
    user = await signup(engine)
    uid = uuid.UUID(user)
    async with engine.begin() as conn:
        for number, local_date in [(0, date(2026, 10, 31)), (1, date(2026, 10, 10)), (2, date(2026, 9, 30))]:
            await conn.execute(text("""
                INSERT INTO daily_practice_offers (user_id, template_id, local_date, options_expire_at)
                SELECT CAST(:u AS uuid), id, CAST(:d AS date), '2026-01-01+00' FROM daily_practice_templates WHERE title_ko = :k
            """), {"u": uid, "d": local_date, "k": f"행동 {number}"})
    body = await call(engine, daily_practice.offer, user, **common())
    assert {option["title"] for option in body["options"]} == {"행동 3", "행동 2", "행동 1"}


async def test_deadline_and_the_day_after(engine):
    await add_templates(engine, 6)
    user = await signup(engine)
    practice_id = str((await call(engine, daily_practice.offer, user, **common()))["options"][0]["id"])
    await call(engine, daily_practice.accept, user, practice_id, **common())

    almost = NOW + timedelta(hours=24) - timedelta(seconds=1)
    live = await call(engine, daily_practice.status, user, **common(now_utc=almost))
    assert str(live["practice"]["id"]) == practice_id and live["can_choose"] is False

    after = NOW + timedelta(hours=24)
    with pytest.raises(AppError) as expired:
        await call(engine, daily_practice.complete, user, practice_id, **common(now_utc=after))
    assert expired.value.code == "DAILY_PRACTICE_EXPIRED"
    next_day = await call(engine, daily_practice.offer, user, **common(now_utc=after))
    assert next_day["practice"] is None and next_day["can_choose"] is True
    assert len(next_day["options"]) == 3


async def test_yesterdays_pick_completed_today_comes_with_todays_options(engine):
    await add_templates(engine, 6)
    user = await signup(engine)
    practice_id = str((await call(engine, daily_practice.offer, user, **common()))["options"][0]["id"])
    await call(engine, daily_practice.accept, user, practice_id, **common())
    tomorrow = NOW + timedelta(hours=20)
    done = await call(engine, daily_practice.complete, user, practice_id, **common(now_utc=tomorrow))
    assert done["local_date"] == date(2026, 11, 2)
    assert str(done["practice"]["id"]) == practice_id and done["can_choose"] is True
    body = await call(engine, daily_practice.offer, user, **common(now_utc=tomorrow))
    assert str(body["practice"]["id"]) == practice_id and len(body["options"]) == 3

    second = str(body["options"][0]["id"])
    await call(engine, daily_practice.accept, user, second, **common(now_utc=tomorrow))
    with pytest.raises(AppError) as blocked:
        await call(engine, daily_practice.uncomplete, user, practice_id, **common(now_utc=tomorrow))
    assert blocked.value.code == "DAILY_PRACTICE_INVALID_STATE"


async def test_soft_deleted_routine_repoints_to_a_new_one(engine):
    await add_templates(engine, 3)
    user = await signup(engine)
    practice_id = await completed(engine, user)
    first = await call(engine, daily_practice.add_routine, user, practice_id, **common())
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE routines SET deleted_at = now() WHERE id = :i"),
                           {"i": uuid.UUID(first["routine"]["id"])})
    assert (await call(engine, daily_practice.status, user, **common()))["practice"]["routine_added"] is False

    second = await call(engine, daily_practice.add_routine, user, practice_id, **common())
    assert second["routine"]["id"] != first["routine"]["id"]
    assert second["today"]["practice"]["routine_added"] is True
    assert str(await scalar(engine, "SELECT routine_id FROM daily_practice_offers WHERE id = :i",
                            i=uuid.UUID(practice_id))) == second["routine"]["id"]


async def test_uncomplete_with_a_soft_deleted_routine_clears_the_link(engine):
    await add_templates(engine, 3)
    user = await signup(engine)
    practice_id = await completed(engine, user)
    added = await call(engine, daily_practice.add_routine, user, practice_id, **common())
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE routines SET deleted_at = now() WHERE id = :i"),
                           {"i": uuid.UUID(added["routine"]["id"])})
    body = await call(engine, daily_practice.uncomplete, user, practice_id, **common())
    assert body["practice"]["status"] == "accepted" and body["practice"]["routine_added"] is False
    assert await scalar(engine, "SELECT routine_id FROM daily_practice_offers WHERE id = :i",
                        i=uuid.UUID(practice_id)) is None


async def test_hard_deleting_a_routine_keeps_the_row_and_clears_the_link(engine):
    await add_templates(engine, 3)
    user = await signup(engine)
    practice_id = await completed(engine, user)
    added = await call(engine, daily_practice.add_routine, user, practice_id, **common())
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM routine_schedules WHERE routine_id = :i"),
                           {"i": uuid.UUID(added["routine"]["id"])})
        await conn.execute(text("DELETE FROM routines WHERE id = :i"),
                           {"i": uuid.UUID(added["routine"]["id"])})
    assert (await call(engine, daily_practice.status, user, **common()))["practice"]["routine_added"] is False


async def test_account_deletion_blocks_writes_removes_rows_and_leaves_templates(engine):
    await add_templates(engine, 3)
    user = await signup(engine)
    uid = uuid.UUID(user)
    practice_id = await completed(engine, user)
    await call(engine, daily_practice.add_routine, user, practice_id, **common())

    async with engine.begin() as conn:
        await conn.execute(text("SELECT public.begin_subject_deletion(:u, :o)"), {"u": uid, "o": uuid.uuid4()})
    for function, args in [
        (daily_practice.status, ()),
        (daily_practice.offer, ()),
        (daily_practice.accept, (practice_id,)),
        (daily_practice.complete, (practice_id,)),
        (daily_practice.uncomplete, (practice_id,)),
        (daily_practice.add_routine, (practice_id,)),
    ]:
        with pytest.raises(AppError) as blocked:
            await call(engine, function, user, *args, **common())
        assert blocked.value.code == "ACCOUNT_DELETING"

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM auth.users WHERE id = :u"), {"u": uid})
    assert await scalar(engine, "SELECT count(*) FROM daily_practice_offers WHERE user_id = :u", u=uid) == 0
    assert await scalar(engine, "SELECT count(*) FROM daily_practice_templates") == 3
