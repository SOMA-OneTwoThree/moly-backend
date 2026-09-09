"""Weekly receipt invariants against the canonical disposable PostgreSQL schema."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import AppError
from app.services import diary_generation, privacy
from db.schema_contract import require_scratch
from db.envfile import load_conn, assert_dev_target

# conftest's autouse fixture stubs this function after module collection.
_REAL_ENSURE_ACTIVE = privacy.ensure_subject_active


@pytest_asyncio.fixture
async def weekly_db(monkeypatch):
    if os.environ.get("MOLY_WEEKLY_TEST_ENV") == "dev":
        if os.environ.get("MOLY_SCHEMA_TEST_DSN"):
            pytest.fail("choose dev or scratch, not both")
        dsn = load_conn("dev")
        assert_dev_target("dev", dsn)
    else:
        dsn = os.environ.get("MOLY_SCHEMA_TEST_DSN")
        if not dsn:
            pytest.skip("MOLY_WEEKLY_TEST_ENV=dev or MOLY_SCHEMA_TEST_DSN required")
        require_scratch(dsn)
    monkeypatch.setattr(privacy, "ensure_subject_active", _REAL_ENSURE_ACTIVE)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=15, command_timeout=30)
    engine = create_async_engine(
        make_url(dsn).set(drivername="postgresql+asyncpg"),
        connect_args={"statement_cache_size": 0},
    )
    users, ments = [], []
    enqueue = diary_generation.diary_recall_repo.jobs.enqueue

    async def enqueue_after_test(*args, **kwargs):
        # Keep the real outbox INSERT, but prevent the development consumer from
        # doing network work for test users before fixture cleanup.
        kwargs["available_at"] = datetime.now(timezone.utc) + timedelta(days=365)
        return await enqueue(*args, **kwargs)

    monkeypatch.setattr(diary_generation.diary_recall_repo.jobs, "enqueue", enqueue_after_test)
    # Isolated, future week; no shared seed rows are modified or removed.
    week = date(4000, 1, 3) + timedelta(weeks=uuid.uuid4().int % 100000)

    async def user():
        uid = uuid.uuid4()
        await conn.execute("INSERT INTO auth.users(id,created_at) VALUES($1,now())", uid)
        users.append(uid)
        await conn.execute("UPDATE profiles SET language='ko',timezone='Asia/Seoul' WHERE id=$1", uid)
        return SimpleNamespace(id=uid, timezone="Asia/Seoul", language="ko", nickname=None)

    async def ment(sequence=1):
        mid = uuid.uuid4()
        await conn.execute(
            "INSERT INTO moly_life_ments(id,content,weather,week_start_date,sequence_no) "
            "VALUES($1,'오늘은 풀밭에서 조용히 쉬었어.','sunny',$2,$3)", mid, week, sequence,
        )
        ments.append(mid)
        return mid

    async def legacy_ment(day):
        mid = uuid.uuid4()
        await conn.execute(
            "INSERT INTO moly_life_ments(id,content,weather,diary_date) "
            "VALUES($1,'날짜로 지정한 과거 운영 원고.','cloudy',$2)", mid, day,
        )
        ments.append(mid)
        return mid

    try:
        yield SimpleNamespace(
            conn=conn, engine=engine, user=user, ment=ment, legacy_ment=legacy_ment, week=week,
        )
    finally:
        for uid in users:
            await conn.execute("DELETE FROM privacy_ledger_events WHERE user_id=$1", uid)
            await conn.execute("DELETE FROM auth.users WHERE id=$1", uid)
            await conn.execute("DELETE FROM privacy_subject_barriers WHERE user_id=$1", uid)
        for mid in ments:
            await conn.execute("DELETE FROM moly_life_ments WHERE id=$1", mid)
        await engine.dispose()
        await conn.close()


async def generate(db, profile, day=None):
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        return await diary_generation.generate_for_user(
            session, profile, day or db.week, {"diary_min_user_chars": 60},
            policy=diary_generation.DiaryPolicy(weekly_start_date=db.week),
        )


async def test_canonical_weekly_constraints_and_legacy_rows(weekly_db):
    db = weekly_db
    profile = await db.user()
    mid = await db.ment()
    for week, sequence, legacy_date in [
        (db.week, None, None), (None, 1, None),
        (db.week + timedelta(days=1), 1, None), (db.week, 0, None),
        (db.week, 2, db.week),
    ]:
        with pytest.raises(asyncpg.CheckViolationError):
            async with db.conn.transaction():
                await db.conn.execute(
                    "INSERT INTO moly_life_ments(content,weather,week_start_date,sequence_no,diary_date) "
                    "VALUES('test','sunny',$1,$2,$3)", week, sequence, legacy_date,
                )
    with pytest.raises(asyncpg.UniqueViolationError):
        async with db.conn.transaction():
            await db.conn.execute(
                "INSERT INTO moly_life_ments(content,weather,week_start_date,sequence_no) "
                "VALUES('duplicate','sunny',$1,1)", db.week,
            )
    for status, preset in [("personal", None), ("preset", None), ("no_entry", mid)]:
        with pytest.raises(asyncpg.CheckViolationError):
            async with db.conn.transaction():
                await db.conn.execute(
                    "INSERT INTO diary_generation_results(user_id,target_date,status,preset_ment_id) "
                    "VALUES($1,$2,$3,$4)", profile.id, db.week, status, preset,
                )
    await db.conn.execute(
        "INSERT INTO diary_generation_results(user_id,target_date,status,preset_ment_id) "
        "VALUES($1,$2,'preset',$3)", profile.id, db.week + timedelta(days=1), mid,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        async with db.conn.transaction():
            await db.conn.execute(
                "INSERT INTO diary_generation_results(user_id,target_date,status,preset_ment_id) "
                "VALUES($1,$2,'preset',$3)", profile.id, db.week + timedelta(days=2), mid,
            )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with db.conn.transaction():
            await db.conn.execute(
                "INSERT INTO diary_generation_results(user_id,target_date,status,preset_ment_id) "
                "VALUES($1,$2,'preset',$3)", profile.id, db.week + timedelta(days=3), uuid.uuid4(),
            )
    # Existing no_entry rows remain valid without supplying the new column.
    await db.conn.execute(
        "INSERT INTO diary_generation_results(user_id,target_date,status) VALUES($1,$2,'no_entry')",
        profile.id, db.week,
    )
    assert (await generate(db, profile))["reason"] == "already_exists"


async def test_user_order_independence_exhaustion_and_diary_deletion(weekly_db):
    db = weekly_db
    a, b = await db.user(), await db.user()
    first, second = await db.ment(1), await db.ment(2)
    result = await generate(db, a)
    assert result["created"] and result["source"] == "preset"
    # The diary snapshot can be hard-deleted without revoking its receipt.
    await db.conn.execute("DELETE FROM diaries WHERE user_id=$1", a.id)
    assert (await generate(db, a))["reason"] == "already_exists"
    await generate(db, a, db.week + timedelta(days=1))
    assert (await generate(db, a, db.week + timedelta(days=2)))["reason"] == "weekly_exhausted"
    await generate(db, b)
    rows = await db.conn.fetch(
        "SELECT preset_ment_id FROM diary_generation_results WHERE user_id=$1 "
        "AND status='preset' ORDER BY target_date", a.id,
    )
    assert [r["preset_ment_id"] for r in rows] == [first, second]
    assert await db.conn.fetchval(
        "SELECT preset_ment_id FROM diary_generation_results WHERE user_id=$1", b.id,
    ) == first
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with db.conn.transaction():
            await db.conn.execute("DELETE FROM moly_life_ments WHERE id=$1", first)


@pytest.mark.parametrize("day_offset", [0, 1])
async def test_concurrent_generation_respects_day_and_receipt_uniqueness(weekly_db, monkeypatch, day_offset):
    db = weekly_db
    profile = await db.user()
    await db.ment(1)
    await db.ment(2)
    original = diary_generation._pick_weekly_ment
    both_ready = asyncio.Event()
    calls = 0

    async def rendezvous(*args, **kwargs):
        nonlocal calls
        candidate = await original(*args, **kwargs)
        calls += 1
        if calls == 2:
            both_ready.set()
        if calls <= 2:
            await asyncio.wait_for(both_ready.wait(), 10)
        return candidate

    monkeypatch.setattr(diary_generation, "_pick_weekly_ment", rendezvous)
    results = await asyncio.wait_for(asyncio.gather(
        generate(db, profile), generate(db, profile, db.week + timedelta(days=day_offset)),
    ), 20)
    expected = 1 + day_offset
    assert sum(r["created"] for r in results) == expected
    assert await db.conn.fetchval("SELECT count(*) FROM diaries WHERE user_id=$1", profile.id) == expected
    assert await db.conn.fetchval(
        "SELECT count(*) FROM diary_generation_results WHERE user_id=$1", profile.id,
    ) == expected


async def test_recall_failure_rolls_back_diary_and_receipt(weekly_db, monkeypatch):
    db = weekly_db
    profile = await db.user()
    mid = await db.ment()
    original = diary_generation.diary_recall_repo.upsert_diary_recall_document

    async def fail_after_enqueue(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected after recall enqueue")

    monkeypatch.setattr(diary_generation.diary_recall_repo, "upsert_diary_recall_document", fail_after_enqueue)
    with pytest.raises(RuntimeError, match="injected"):
        await generate(db, profile)
    for table in ("diaries", "diary_generation_results", "diary_recall_documents", "async_jobs"):
        assert await db.conn.fetchval(f"SELECT count(*) FROM {table} WHERE user_id=$1", profile.id) == 0
    monkeypatch.setattr(diary_generation.diary_recall_repo, "upsert_diary_recall_document", original)
    await generate(db, profile)
    assert await db.conn.fetchval(
        "SELECT preset_ment_id FROM diary_generation_results WHERE user_id=$1", profile.id,
    ) == mid


async def test_deletion_barrier_blocks_generation_and_account_cascades_receipt(weekly_db):
    db = weekly_db
    profile = await db.user()
    await db.ment()
    await generate(db, profile)
    await db.conn.execute(
        "UPDATE privacy_subject_barriers SET state='deleting',operation_id=$2 WHERE user_id=$1",
        profile.id, uuid.uuid4(),
    )
    with pytest.raises(AppError) as exc:
        await generate(db, profile, db.week + timedelta(days=1))
    assert getattr(exc.value, "code", None) == "ACCOUNT_DELETING"
    await db.conn.execute("DELETE FROM auth.users WHERE id=$1", profile.id)
    assert await db.conn.fetchval(
        "SELECT count(*) FROM diary_generation_results WHERE user_id=$1", profile.id,
    ) == 0


async def test_no_entry_rechecks_inventory_before_commit(weekly_db, monkeypatch):
    db = weekly_db
    profile = await db.user()
    original = diary_generation._pick_weekly_ment
    inserted = False

    async def add_after_initial_empty(*args, **kwargs):
        nonlocal inserted
        candidate = await original(*args, **kwargs)
        if candidate is None and not inserted:
            inserted = True
            await db.ment()
        return candidate

    monkeypatch.setattr(diary_generation, "_pick_weekly_ment", add_after_initial_empty)
    result = await generate(db, profile)
    assert result["created"] and result["source"] == "preset"
    assert await db.conn.fetchval(
        "SELECT status FROM diary_generation_results WHERE user_id=$1", profile.id,
    ) == "preset"


async def test_withdrawn_candidate_is_not_consumed(weekly_db, monkeypatch):
    db = weekly_db
    profile = await db.user()
    first, second = await db.ment(1), await db.ment(2)
    original = diary_generation._pick_weekly_ment
    withdrawn = False

    async def withdraw_after_initial_pick(*args, **kwargs):
        nonlocal withdrawn
        candidate = await original(*args, **kwargs)
        if candidate is not None and not withdrawn:
            withdrawn = True
            await db.conn.execute("UPDATE moly_life_ments SET is_active=false WHERE id=$1", first)
        return candidate

    monkeypatch.setattr(diary_generation, "_pick_weekly_ment", withdraw_after_initial_pick)
    assert (await generate(db, profile))["created"]
    assert await db.conn.fetchval(
        "SELECT preset_ment_id FROM diary_generation_results WHERE user_id=$1", profile.id,
    ) == second


async def test_personal_day_preserves_first_operator_entry_for_next_day(weekly_db, monkeypatch):
    db = weekly_db
    profile = await db.user()
    first = await db.ment(1)
    user_content = "오늘 친구와 공원에서 즐겁게 산책하고 이야기를 나누었어. " * 3
    message_id = await db.conn.fetchval(
        "INSERT INTO messages(user_id,sender,content,activity_date) "
        "VALUES($1,'user',$2,$3) RETURNING id", profile.id, user_content, db.week,
    )
    personal_calls = []

    async def personal(snapshot, messages, **_kwargs):
        personal_calls.append((snapshot.id, [message.id for message in messages]))
        return ("오늘은 함께 공원을 산책했다.", "sunny"), {
            "empty_body": False, "self_check_passed": True,
        }

    monkeypatch.setattr(diary_generation, "_personal", personal)
    result = await generate(db, profile)
    assert result["source"] == "llm" and result["gate_passed"]
    assert personal_calls == [(profile.id, [message_id])]
    assert await db.conn.fetchval(
        "SELECT count(*) FROM diary_generation_results WHERE user_id=$1", profile.id,
    ) == 0
    assert await db.conn.fetchval(
        "SELECT message_id FROM diary_claim_sources WHERE user_id=$1", profile.id,
    ) == message_id
    next_day = await generate(db, profile, db.week + timedelta(days=1))
    assert next_day["source"] == "preset"
    assert await db.conn.fetchval(
        "SELECT preset_ment_id FROM diary_generation_results WHERE user_id=$1", profile.id,
    ) == first
    assert len(personal_calls) == 1


async def test_cutover_preserves_legacy_and_never_falls_back_to_dated_entry(weekly_db):
    db = weekly_db
    profile = await db.user()
    previous_day = db.week - timedelta(days=1)
    legacy = await db.legacy_ment(previous_day)
    await db.legacy_ment(db.week)
    previous = await generate(db, profile, previous_day)
    assert previous["source"] == "preset"
    assert await db.conn.fetchval(
        "SELECT preset_ment_id FROM diaries WHERE user_id=$1 AND activity_date=$2",
        profile.id, previous_day,
    ) == legacy
    assert await db.conn.fetchval(
        "SELECT count(*) FROM diary_generation_results WHERE user_id=$1", profile.id,
    ) == 0
    weekly = await generate(db, profile)
    assert weekly["reason"] == "weekly_unavailable"
    assert await db.conn.fetchval(
        "SELECT count(*) FROM diaries WHERE user_id=$1 AND activity_date=$2", profile.id, db.week,
    ) == 0
    first = await db.ment(1)
    assert (await generate(db, profile))["reason"] == "already_exists"
    await generate(db, profile, db.week + timedelta(days=1))
    assert await db.conn.fetchval(
        "SELECT preset_ment_id FROM diary_generation_results WHERE user_id=$1 AND status='preset'",
        profile.id,
    ) == first


async def test_different_users_share_candidate_lock_without_serializing(weekly_db, monkeypatch):
    db = weekly_db
    a, b = await db.user(), await db.user()
    first = await db.ment(1)
    original = diary_generation._pick_weekly_ment
    both_locked = asyncio.Event()
    locked_users = set()

    async def hold_shared_lock(session, user_id, week_start, *, lock=False):
        candidate = await original(session, user_id, week_start, lock=lock)
        if lock:
            # Both transactions must own their candidate lock simultaneously.
            # An accidental FOR UPDATE would block the second until timeout.
            assert candidate.id == first
            locked_users.add(user_id)
            if locked_users == {a.id, b.id}:
                both_locked.set()
            await asyncio.wait_for(both_locked.wait(), 10)
        return candidate

    monkeypatch.setattr(diary_generation, "_pick_weekly_ment", hold_shared_lock)
    results = await asyncio.wait_for(asyncio.gather(generate(db, a), generate(db, b)), 20)
    assert all(result["created"] for result in results)
    assert locked_users == {a.id, b.id}
    assert await db.conn.fetchval(
        "SELECT count(*) FROM diary_generation_results WHERE user_id=ANY($1::uuid[]) "
        "AND preset_ment_id=$2", [a.id, b.id], first,
    ) == 2


async def test_privacy_deletion_between_preparation_and_finalization_blocks_receipt(
    weekly_db, monkeypatch,
):
    db = weekly_db
    profile = await db.user()
    await db.ment()
    original = diary_generation._lock_and_check
    prepared = asyncio.Event()
    deletion_committed = asyncio.Event()

    async def wait_for_deletion(session, user_id, target_date):
        prepared.set()
        await asyncio.wait_for(deletion_committed.wait(), 10)
        return await original(session, user_id, target_date)

    monkeypatch.setattr(diary_generation, "_lock_and_check", wait_for_deletion)

    async def delete_after_preparation():
        await asyncio.wait_for(prepared.wait(), 10)
        async with AsyncSession(db.engine, expire_on_commit=False) as session:
            await privacy.begin_subject_deletion(
                session, user_id=profile.id, operation_id=uuid.uuid4(),
            )
            await session.commit()
        deletion_committed.set()

    async def publish_after_preparation():
        with pytest.raises(AppError) as exc:
            await generate(db, profile)
        assert exc.value.code == "ACCOUNT_DELETING"

    await asyncio.wait_for(asyncio.gather(
        delete_after_preparation(), publish_after_preparation(),
    ), 20)
    for table in ("diaries", "diary_generation_results", "diary_recall_documents"):
        assert await db.conn.fetchval(f"SELECT count(*) FROM {table} WHERE user_id=$1", profile.id) == 0
    assert await db.conn.fetchval(
        "SELECT count(*) FROM async_jobs WHERE user_id=$1 AND job_type='diary_recall_embed'",
        profile.id,
    ) == 0
