"""Actual-development database checks for cross-account fortune determinism."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import uuid

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.schemas.fortune import FortuneProfilePut
from app.services import fortune, privacy
from db.envfile import assert_dev_target, load_conn
from db.schema_contract import require_scratch

_REAL_ENSURE_ACTIVE = privacy.ensure_subject_active

NOW = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def experience_db(monkeypatch):
    if os.environ.get("MOLY_FORTUNE_TEST_ENV") == "dev":
        if os.environ.get("MOLY_SCHEMA_TEST_DSN"):
            pytest.fail("choose dev or scratch, not both")
        dsn = load_conn("dev")
        assert_dev_target("dev", dsn)
    else:
        dsn = os.environ.get("MOLY_SCHEMA_TEST_DSN")
        if not dsn:
            pytest.skip("MOLY_FORTUNE_TEST_ENV=dev or approved scratch DSN required")
        require_scratch(dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=15, command_timeout=30)
    engine = create_async_engine(make_url(dsn).set(drivername="postgresql+asyncpg"),
                                 connect_args={"statement_cache_size": 0})
    uids = [uuid.uuid4(), uuid.uuid4()]
    monkeypatch.setattr(fortune, "_ready", lambda: True)
    monkeypatch.setattr(fortune.settings, "environment", "development")

    async def included(*_args, **_kwargs):
        return "included", "monthly"

    monkeypatch.setattr(fortune, "_access", included)
    # Restore the actual barrier check if global test fixtures mock it.
    monkeypatch.setattr(privacy, "ensure_subject_active", _REAL_ENSURE_ACTIVE)
    try:
        for uid, zone in zip(uids, ("UTC", "Asia/Seoul")):
            await conn.execute("INSERT INTO auth.users(id,created_at) VALUES($1,now())", uid)
            await conn.execute("UPDATE profiles SET timezone=$2,language='ko' WHERE id=$1", uid, zone)
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await fortune.put_profile(session, str(uid),
                    FortuneProfilePut(birth_date=date(2002, 12, 13), gender="undisclosed"), now_utc=NOW)
        yield SimpleNamespace(conn=conn, engine=engine, uids=uids)
    finally:
        for uid in uids:
            await conn.execute("DELETE FROM privacy_ledger_events WHERE user_id=$1", uid)
            await conn.execute("DELETE FROM auth.users WHERE id=$1", uid)
            await conn.execute("DELETE FROM privacy_subject_barriers WHERE user_id=$1", uid)
            assert not await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=$1 UNION ALL "
                "SELECT 1 FROM fortune_profiles WHERE user_id=$1 UNION ALL "
                "SELECT 1 FROM daily_fortunes WHERE user_id=$1 UNION ALL "
                "SELECT 1 FROM privacy_ledger_events WHERE user_id=$1 UNION ALL "
                "SELECT 1 FROM privacy_subject_barriers WHERE user_id=$1)", uid)
        await engine.dispose()
        await conn.close()


async def _reveal(db, uid, *, now=NOW, locale="ko"):
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        return await fortune.reveal(session, str(uid), locale=locale, now_utc=now)


def _scores(value):
    result = value["result"]
    return [result["overall"]["score"], *[result["categories"][a]["score"]
            for a in ("love", "money", "work", "energy")]]


async def test_first_and_regular_are_identical_between_accounts_in_all_locales(experience_db):
    db = experience_db
    for offset in (0, 1):
        day = NOW + timedelta(days=offset)
        for locale in ("ko", "en", "ja"):
            left, right = await asyncio.gather(*[
                _reveal(db, uid, now=day, locale=locale) for uid in db.uids
            ])
            assert left["result"] == right["result"]
            assert all((90 if offset == 0 else 60) <= s <= 100 for s in _scores(left))
            assert sum(s < 70 for s in _scores(left)) <= (0 if offset == 0 else 1)
            text = json.dumps(left["result"])
            assert '"astrology"' not in text
            assert '"card_id"' not in text
            assert '"experience_mode"' not in text
        stored = [json.loads(await db.conn.fetchval(
            "SELECT semantic_result FROM daily_fortunes WHERE user_id=$1", uid,
        )) for uid in db.uids]
        assert stored[0] == stored[1]
        assert stored[0]["experience_mode"] == ("first_visit" if offset == 0 else "regular")


async def test_parallel_first_creation_and_profile_edit_recalculate_issued_result(experience_db):
    db = experience_db
    uid = db.uids[0]
    results = await asyncio.gather(*[_reveal(db, uid) for _ in range(4)])
    assert all(r["result"] == results[0]["result"] for r in results)
    assert all(90 <= s <= 100 for s in _scores(results[0]))
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        edited = await fortune.put_profile(session, str(uid),
            FortuneProfilePut(birth_date=date(1998, 5, 21), gender="woman"), now_utc=NOW)
    assert edited["result_invalidated"] is True
    assert edited["unlock_preserved"] is True
    recalculated = await asyncio.gather(*[_reveal(db, uid) for _ in range(4)])
    assert all(r["result"] == recalculated[0]["result"] for r in recalculated)
    assert recalculated[0]["result"] != results[0]["result"]
    assert all(90 <= value <= 100 for value in _scores(recalculated[0]))
    assert await db.conn.fetchval("SELECT fortune_first_date FROM profiles WHERE id=$1", uid) == NOW.date()
    assert await db.conn.fetchval("SELECT count(*) FROM daily_fortunes WHERE user_id=$1", uid) == 1
    next_day = await _reveal(db, uid, now=NOW + timedelta(days=1))
    assert next_day["result"] != results[0]["result"]


@pytest.mark.parametrize("first_visit", (True, False))
@pytest.mark.parametrize("unlock_source", ("subscription", "rewarded_ad"))
async def test_birth_edit_round_trip_recalculates_all_locales_and_preserves_unlock(
    experience_db, first_visit, unlock_source,
):
    db = experience_db
    uid, peer = db.uids
    # Both accounts have the same first-use day, independently of their timezone.
    await asyncio.gather(*[_reveal(db, user) for user in db.uids])
    now = NOW if first_visit else NOW + timedelta(days=1)
    locales = ("ko", "en", "ja")
    original = {locale: (await _reveal(db, uid, now=now, locale=locale))["result"]
                for locale in locales}
    # Seed the existing unlock source explicitly: recalculation must preserve an
    # earned ad unlock as well as a subscription unlock, without another reward.
    await db.conn.execute(
        "UPDATE daily_fortunes SET unlock_source=$2 WHERE user_id=$1", uid, unlock_source,
    )
    unlocked_at = await db.conn.fetchval("SELECT unlocked_at FROM daily_fortunes WHERE user_id=$1", uid)
    marker = await db.conn.fetchval("SELECT fortune_first_date FROM profiles WHERE id=$1", uid)

    async def put(user, birth):
        async with AsyncSession(db.engine, expire_on_commit=False) as session:
            return await fortune.put_profile(session, str(user),
                FortuneProfilePut(birth_date=birth, gender="undisclosed"), now_utc=now)

    changed = await put(uid, date(1998, 5, 21))
    assert changed["profile"]["revision"] == 2
    assert changed["result_invalidated"] is True
    assert changed["unlock_preserved"] is True
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        pending = await fortune.status(session, str(uid), locale="ko", now_utc=now)
    assert pending["state"] == "unseen"
    assert "result" not in pending

    requested_locales = locales * 2
    recalculated = await asyncio.gather(*[
        _reveal(db, uid, now=now, locale=locale) for locale in requested_locales
    ])
    by_locale = {}
    for locale, response in zip(requested_locales, recalculated):
        assert response["state"] == "revealed"
        assert response["local_date"] == now.date()
        assert response["result"] == by_locale.setdefault(locale, response["result"])
        assert all((90 if first_visit else 60) <= score <= 100 for score in _scores(response))
    assert by_locale != original
    stored = await db.conn.fetchrow(
        "SELECT profile_revision,unlock_state,unlock_source,unlocked_at,semantic_result "
        "FROM daily_fortunes WHERE user_id=$1", uid,
    )
    assert stored["profile_revision"] == 2
    assert (stored["unlock_state"], stored["unlock_source"], stored["unlocked_at"]) == (
        "unlocked", unlock_source, unlocked_at,
    )
    semantic = json.loads(stored["semantic_result"])
    assert semantic["experience_mode"] == ("first_visit" if first_visit else "regular")
    assert await db.conn.fetchval("SELECT fortune_first_date FROM profiles WHERE id=$1", uid) == marker
    assert await db.conn.fetchval("SELECT count(*) FROM daily_fortunes WHERE user_id=$1", uid) == 1

    # Another account reaching B independently must receive precisely the same
    # result; neither revision number nor the old A snapshot is draw entropy.
    await put(peer, date(1998, 5, 21))
    for locale in locales:
        assert (await _reveal(db, peer, now=now, locale=locale))["result"] == by_locale[locale]

    unchanged = await put(uid, date(1998, 5, 21))
    assert unchanged["profile"]["revision"] == 2
    assert unchanged["result_invalidated"] is False
    restored = await put(uid, date(2002, 12, 13))
    assert restored["profile"]["revision"] == 3
    assert restored["result_invalidated"] is True
    assert restored["unlock_preserved"] is True
    for locale in locales:
        assert (await _reveal(db, uid, now=now, locale=locale))["result"] == original[locale]
    after = await db.conn.fetchrow(
        "SELECT profile_revision,unlock_state,unlock_source,unlocked_at FROM daily_fortunes WHERE user_id=$1", uid,
    )
    assert tuple(after.values()) == (3, "unlocked", unlock_source, unlocked_at)
    assert await db.conn.fetchval("SELECT fortune_first_date FROM profiles WHERE id=$1", uid) == marker


async def test_failed_first_save_does_not_consume_first_visit(experience_db):
    db = experience_db
    uid = db.uids[0]

    class FailingSession(AsyncSession):
        async def commit(self):
            await self.flush()
            raise RuntimeError("injected first save failure")

    async with FailingSession(db.engine, expire_on_commit=False) as session:
        with pytest.raises(RuntimeError, match="injected first save failure"):
            await fortune.reveal(session, str(uid), locale="ko", now_utc=NOW)
        await session.rollback()
    assert await db.conn.fetchval("SELECT fortune_first_date FROM profiles WHERE id=$1", uid) is None
    assert not await db.conn.fetchval("SELECT EXISTS(SELECT 1 FROM daily_fortunes WHERE user_id=$1)", uid)
    result = await _reveal(db, uid)
    assert all(90 <= s <= 100 for s in _scores(result))
    assert await db.conn.fetchval("SELECT fortune_first_date FROM profiles WHERE id=$1", uid) == NOW.date()


async def test_profile_deletion_does_not_reset_first_visit_marker(experience_db):
    db = experience_db
    uid = db.uids[0]
    first = await _reveal(db, uid)
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        await fortune.delete_profile(session, str(uid))
    assert await db.conn.fetchval("SELECT fortune_first_date FROM profiles WHERE id=$1", uid) == NOW.date()
    assert not await db.conn.fetchval("SELECT EXISTS(SELECT 1 FROM daily_fortunes WHERE user_id=$1)", uid)
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        await fortune.put_profile(session, str(uid),
            FortuneProfilePut(birth_date=date(2002, 12, 13), gender="undisclosed"), now_utc=NOW)
    assert (await _reveal(db, uid))["result"] == first["result"]
    await _reveal(db, uid, now=NOW + timedelta(days=1))
    stored = json.loads(await db.conn.fetchval("SELECT semantic_result FROM daily_fortunes WHERE user_id=$1", uid))
    assert stored["experience_mode"] == "regular"
    assert await db.conn.fetchval("SELECT fortune_first_date FROM profiles WHERE id=$1", uid) == NOW.date()


async def test_same_calendar_day_timezone_edit_does_not_redraw(experience_db):
    db = experience_db
    uid = db.uids[0]
    before = await _reveal(db, uid)
    await db.conn.execute("UPDATE profiles SET timezone='Asia/Seoul' WHERE id=$1", uid)
    after = await _reveal(db, uid)
    assert before["result"] == after["result"]


async def test_production_gate_preserves_legacy_day_then_activates_without_first_bonus(experience_db, monkeypatch):
    from app.services import config_store
    db = experience_db
    uid = db.uids[0]
    monkeypatch.setattr(fortune.settings, "environment", "production")
    values = {}

    async def config(*_args, **_kwargs):
        return values

    monkeypatch.setattr(config_store, "get_config_values", config)
    legacy = await _reveal(db, uid)
    stored_before = json.loads(await db.conn.fetchval("SELECT semantic_result FROM daily_fortunes WHERE user_id=$1", uid))
    assert "experience_version" not in stored_before
    values["fortune_experience_start_date"] = NOW.date().isoformat()
    assert (await _reveal(db, uid))["result"] == legacy["result"]
    assert await db.conn.fetchval("SELECT fortune_first_date FROM profiles WHERE id=$1", uid) == date.min
    await _reveal(db, uid, now=NOW + timedelta(days=1))
    stored = json.loads(await db.conn.fetchval("SELECT semantic_result FROM daily_fortunes WHERE user_id=$1", uid))
    assert stored["experience_mode"] == "regular"


@pytest.mark.parametrize("activation", [None, True, "bad-date", "20260912", "2026-09-13"])
async def test_production_gate_rejects_unset_malformed_and_future_dates(experience_db, monkeypatch, activation):
    from app.services import config_store
    db = experience_db
    monkeypatch.setattr(fortune.settings, "environment", "production")

    async def config(*_args, **_kwargs):
        return {"fortune_experience_start_date": activation}

    monkeypatch.setattr(config_store, "get_config_values", config)
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        assert not await fortune._experience_enabled(session, NOW.date())


async def test_gate_off_legacy_delete_cannot_reset_first_marker(experience_db, monkeypatch):
    from app.services import config_store
    db = experience_db
    uid = db.uids[0]
    monkeypatch.setattr(fortune.settings, "environment", "production")
    values = {}

    async def config(*_args, **_kwargs):
        return values

    monkeypatch.setattr(config_store, "get_config_values", config)
    await _reveal(db, uid)
    assert await db.conn.fetchval("SELECT fortune_first_date FROM profiles WHERE id=$1", uid) == date.min
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        await fortune.delete_profile(session, str(uid))
    values["fortune_experience_start_date"] = NOW.date().isoformat()
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        await fortune.put_profile(session, str(uid),
            FortuneProfilePut(birth_date=date(2002, 12, 13), gender="undisclosed"), now_utc=NOW)
    await _reveal(db, uid)
    stored = json.loads(await db.conn.fetchval("SELECT semantic_result FROM daily_fortunes WHERE user_id=$1", uid))
    assert stored["experience_mode"] == "regular"
