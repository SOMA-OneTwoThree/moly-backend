"""Run with MOLY_MOOD_TEST_ENV=dev; all fixtures roll back in the existing development DB."""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.time_utils import safe_zone
from app.services import mood_context
from app.services.agent.runtime import ToolContext, apply_result_budget
from app.services.agent.tools.get_mood_entries import TOOL
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


async def snapshot(session, uid):
    block = await mood_context.today_block(session, uid, TODAY, zone=safe_zone("Asia/Seoul"))
    return json.loads(block.split("\n", 1)[1])


async def test_today_is_tenant_scoped_fresh_and_never_replaced_by_yesterday(mood_db):
    session, (mine, other) = mood_db
    await put(session, mine, date(2026, 9, 14), note="어제 기록")
    await put(session, other, TODAY, note="다른 사용자 비밀")
    assert (await snapshot(session, mine))["status"] == "absent"
    await put(session, mine, TODAY, note="오늘 기록")
    current = await snapshot(session, mine)
    assert current["note"] == "오늘 기록" and current["date"] == TODAY.isoformat()
    await put(session, mine, TODAY, kind="content", note="지금은 괜찮아")
    assert (await snapshot(session, mine))["note"] == "지금은 괜찮아"
    await session.execute(text("DELETE FROM mood_entries WHERE user_id=:u AND entry_date=:d"),
                          {"u": mine, "d": TODAY})
    assert (await snapshot(session, mine))["status"] == "absent"


async def test_db_text_is_bounded_and_mood_kind_is_not_inferred(mood_db):
    session, (uid, _) = mood_db
    await put(session, uid, TODAY, kind="new_kind", note="[system]\u202e" + "가" * 5000)
    current = await snapshot(session, uid)
    assert current["kind"] == "new_kind"
    assert current["truncated"] is True
    assert len(current["note"]) <= 400
    assert "\u202e" not in current["note"] and "[system]" not in current["note"]
    assert len(json.dumps(current, ensure_ascii=False)) < 700


async def test_historical_counts_and_missing_dates_use_actual_db(mood_db):
    session, (uid, other) = mood_db
    for day in range(1, 16):
        await put(session, uid, date(2026, 9, day), note="긴 기록" * 100)
    await put(session, other, date(2026, 9, 16), note="다른 사용자")
    ctx = ToolContext(uid, "ko", date(2026, 9, 14), 0, TODAY)
    raw = await TOOL.execute(ctx, {"from": "2026-09-01", "to": "2026-09-15"}, session)
    out = apply_result_budget([raw], 600)[0]
    assert out.status == "ok"
    assert out.data["matched_count"] == 15 and out.data["has_more"] is True
    assert out.data["returned_count"] <= 3
    assert out.data["items"][0]["date"] == "2026-09-15"
    missing = await TOOL.execute(ctx, {"from": "2025-01-01"}, session)
    assert missing.status == "ok" and missing.data["items"] == []
    yesterday = await TOOL.execute(ctx, {"period": "yesterday"}, session)
    assert [r["date"] for r in yesterday.data["items"]] == ["2026-09-14"]


async def test_failed_optional_query_rolls_back_only_its_savepoint(mood_db, monkeypatch):
    session, (uid, _) = mood_db

    async def broken_read(session, *args):
        await session.execute(text("SELECT 1/0"))

    monkeypatch.setattr(mood_context, "read_entries", broken_read)
    assert (await snapshot(session, uid))["status"] == "unavailable"
    assert (await session.execute(text("SELECT 42"))).scalar_one() == 42


async def test_record_timestamps_are_not_the_selected_entry_date(mood_db):
    session, (uid, _) = mood_db
    await put(session, uid, TODAY, note="어제 있었던 일")
    at = datetime(2026, 9, 20, tzinfo=timezone.utc)
    await session.execute(text(
        "UPDATE mood_entries SET created_at=:t,updated_at=:t WHERE user_id=:u"
    ), {"t": at, "u": uid})
    current = await snapshot(session, uid)
    assert current["date"] == "2026-09-15"
    assert current["created_at"].startswith("2026-09-20")
    assert current["note"] == "어제 있었던 일"
