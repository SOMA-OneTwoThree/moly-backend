"""Snapshot/cursor atomicity against an explicitly selected PostgreSQL database."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import os
import re
from types import SimpleNamespace
import uuid

import asyncpg
from fastapi.encoders import jsonable_encoder
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.fortune import DailyFortune
from app.schemas.fortune import FortuneProfilePut
from app.services import fortune, fortune_catalog, fortune_scores, privacy
from app.services.fortune_copy_selection import draw_cards
from db.envfile import assert_dev_target, load_conn
from db.schema_contract import require_scratch

_REAL_GENERATE_RESULT = fortune_scores.generate_result
_REAL_ENSURE_ACTIVE = privacy.ensure_subject_active
DAY = date(2026, 9, 8)
NOW = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)


def _legacy_semantic():
    return {
        "schema_version": 3,
        "overall": {
            "score": 60,
            "reading_code": "overall.d60.start.clear",
            "expression_route": "overall.d60.start.default",
        },
        "categories": {
            category: {
                "score": 50,
                "reading_code": f"category.{category}.d50.general.clear",
                "expression_route": f"category.{category}.d50.general",
            }
            for category in ("love", "money", "work", "energy")
        },
        "lucky_color_key": "coral",
    }


def _semantic():
    return {
        "schema_version": 4,
        "overall": {"score": 60, "reading_code": "overall.d60.general", "expression_route": "overall.d60.general"},
        "categories": {axis: {"score": 50, "reading_code": f"category.{axis}.d50.general.clear",
                              "expression_route": f"category.{axis}.d50.general"}
                       for axis in ("love", "money", "work", "energy")},
        "lucky_color_key": fortune_scores.COLORS[6],
    }


@pytest_asyncio.fixture
async def fortune_db(monkeypatch):
    if os.environ.get("MOLY_FORTUNE_TEST_ENV") == "dev":
        if os.environ.get("MOLY_SCHEMA_TEST_DSN"):
            pytest.fail("choose dev or scratch, not both")
        dsn = load_conn("dev")
        assert_dev_target("dev", dsn)
    else:
        dsn = os.environ.get("MOLY_SCHEMA_TEST_DSN")
        if not dsn:
            pytest.skip("MOLY_FORTUNE_TEST_ENV=dev or MOLY_SCHEMA_TEST_DSN required")
        require_scratch(dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=15, command_timeout=30)
    engine = create_async_engine(
        make_url(dsn).set(drivername="postgresql+asyncpg"),
        connect_args={"statement_cache_size": 0},
    )
    uid = uuid.uuid4()
    monkeypatch.setattr(privacy, "ensure_subject_active", _REAL_ENSURE_ACTIVE)
    monkeypatch.setattr(fortune, "_ready", lambda: True)
    monkeypatch.setattr(fortune.settings, "environment", "development")
    # Pin score calculation to revisit one route reliably.
    monkeypatch.setattr(fortune_scores, "generate_result", lambda **_: _semantic())

    async def included(*_args, **_kwargs):
        return "included", "monthly"

    monkeypatch.setattr(fortune, "_access", included)
    try:
        await conn.execute("INSERT INTO auth.users(id,created_at) VALUES($1,now())", uid)
        await conn.execute("UPDATE profiles SET timezone='UTC',language='ko',fortune_first_date=DATE '0001-01-01' WHERE id=$1", uid)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await fortune.put_profile(
                session, str(uid),
                FortuneProfilePut(birth_date=date(2002, 12, 13), gender="undisclosed"),
                now_utc=NOW,
            )
        yield SimpleNamespace(conn=conn, engine=engine, uid=uid)
    finally:
        await conn.execute("DELETE FROM privacy_ledger_events WHERE user_id=$1", uid)
        await conn.execute("DELETE FROM auth.users WHERE id=$1", uid)
        await conn.execute("DELETE FROM privacy_subject_barriers WHERE user_id=$1", uid)
        remaining = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=$1 "
            "UNION ALL SELECT 1 FROM fortune_profiles WHERE user_id=$1 "
            "UNION ALL SELECT 1 FROM daily_fortunes WHERE user_id=$1 "
            "UNION ALL SELECT 1 FROM privacy_ledger_events WHERE user_id=$1 "
            "UNION ALL SELECT 1 FROM privacy_subject_barriers WHERE user_id=$1)", uid,
        )
        await engine.dispose()
        await conn.close()
        assert not remaining, "temporary fortune test data was not fully removed"


async def _reveal(db, *, now=NOW, locale="ko"):
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        return await fortune.reveal(session, str(db.uid), locale=locale, now_utc=now)


async def _stored(db):
    return json.loads(await db.conn.fetchval(
        "SELECT semantic_result FROM daily_fortunes WHERE user_id=$1", db.uid,
    ))


async def test_parallel_reveal_allocates_once_and_locale_only_changes_copy(fortune_db):
    db = fortune_db
    left, right = await asyncio.gather(_reveal(db), _reveal(db))
    assert left["result"] == right["result"]
    stored = await _stored(db)
    assert stored["copy_selection"] == draw_cards(birth_date=date(2002, 12, 13), today=DAY)
    for locale in ("ko", "en", "ja"):
        result = await _reveal(db, locale=locale)
        semantic = {k: v for k, v in stored.items() if k != "copy_selection"}
        expected = fortune_catalog.load_catalog().render_cards(
            semantic, locale, selection=stored["copy_selection"],
        )
        assert result["result"]["overall"]["headline"] == expected["overall"]["headline"]
        assert await _stored(db) == stored
        assert "copy_selection" not in result["result"]
        public_payload = json.dumps(jsonable_encoder(result), ensure_ascii=False)
        assert not re.search(r"타로|タロット|\btarot\b|major\.[a-z_]+|(?:cups|swords|wands|pentacles)\.\d", public_payload, re.I)
        assert not any(key in public_payload for key in ('"card_id"', '"orientation"', '"observations"', '"copy_sha256"'))


async def test_new_date_draw_and_same_day_profile_edit_and_date_rollback(fortune_db):
    db = fortune_db
    await _reveal(db)
    first = await _stored(db)
    await _reveal(db, now=NOW + timedelta(days=1))
    second = await _stored(db)
    assert first["overall"] == second["overall"]
    assert second["copy_selection"] == draw_cards(birth_date=date(2002, 12, 13), today=DAY + timedelta(days=1))
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        await fortune.put_profile(
            session, str(db.uid),
            FortuneProfilePut(birth_date=date(2002, 12, 13), gender="woman"),
            now_utc=NOW + timedelta(days=1),
        )
    await _reveal(db, now=NOW + timedelta(days=1))
    assert await _stored(db) == second
    await _reveal(db, now=NOW)
    assert (await _stored(db))["copy_selection"] == first["copy_selection"]
    await _reveal(db, now=NOW + timedelta(days=1))
    assert (await _stored(db))["copy_selection"] == second["copy_selection"]


async def test_flush_then_failed_commit_does_not_consume_a_variant(fortune_db):
    db = fortune_db
    await _reveal(db)
    before = await _stored(db)

    class FailingSession(AsyncSession):
        async def commit(self):
            await self.flush()
            raise RuntimeError("injected commit failure")

    async with FailingSession(db.engine, expire_on_commit=False) as session:
        with pytest.raises(RuntimeError, match="injected commit failure"):
            await fortune.reveal(
                session, str(db.uid), locale="ko", now_utc=NOW + timedelta(days=1),
            )
        await session.rollback()
    assert await _stored(db) == before
    await _reveal(db, now=NOW + timedelta(days=1))
    after = await _stored(db)
    assert after["copy_selection"] == draw_cards(birth_date=date(2002, 12, 13), today=DAY + timedelta(days=1))


async def test_legacy_snapshot_and_coral_stay_fixed_until_new_date(fortune_db):
    db = fortune_db
    await _reveal(db)
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        row = await session.get(DailyFortune, db.uid)
        row.semantic_result = {k: v for k, v in row.semantic_result.items() if k != "copy_selection"}
        copies = deepcopy(row.copy_by_locale)
        for locale, name in (("ko", "코랄"), ("en", "Coral"), ("ja", "コーラル")):
            copies[locale]["lucky_color"] = {"key": "coral", "name": name, "hex": "#FF7F6E"}
        row.copy_by_locale = copies
        row.copy_version = "fortune-copy.v2-initial.1"
        await session.commit()
    result = await _reveal(db)
    assert result["result"]["lucky_color"]["name"] == "코랄"
    assert result["versions"]["copy"] == "fortune-copy.v2-initial.1"
    assert "copy_selection" not in await _stored(db)
    result = await _reveal(db, now=NOW + timedelta(days=1))
    assert result["result"]["lucky_color"]["key"] == fortune_scores.COLORS[6]
    assert result["result"]["lucky_color"]["name"] != "코랄"
    assert "copy_selection" in await _stored(db)


async def test_profile_deletion_cascades_cursor_with_snapshot(fortune_db):
    db = fortune_db
    await _reveal(db)
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        await fortune.delete_profile(session, str(db.uid))
    assert await db.conn.fetchval("SELECT count(*) FROM daily_fortunes WHERE user_id=$1", db.uid) == 0


async def test_type_based_cursor_upgrades_once_and_resets_rewritten_categories(fortune_db):
    from hashlib import sha256
    from app.services.fortune_copy_selection import VARIANT_IDS

    db = fortune_db
    before = await _reveal(db)
    old = _legacy_semantic()
    source_routes = {'overall': old['overall']['expression_route']}
    source_routes.update({k: v['expression_route'] for k, v in old['categories'].items()})
    selected = {}
    for key, route in source_routes.items():
        order = sorted(VARIANT_IDS, key=lambda v: (
            sha256(f'fortune-selection.v1|{route}|{v}'.encode('ascii')).digest(), v,
        ))
        selected[key] = order[7]
    old['copy_selection'] = {
        'version': 'fortune-selection.v1', 'selected': selected,
        'routes': {r: {'day': DAY.isoformat(), 'position': 7} for r in source_routes.values()},
    }
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        row = await session.get(DailyFortune, db.uid)
        row.semantic_result = old
        row.copy_version = 'fortune-copy.v2-variants.1'
        await session.commit()
    same_day = await _reveal(db)
    assert same_day['result'] == before['result']
    assert (await _stored(db))['copy_selection']['version'] == 'fortune-selection.v1'
    await _reveal(db, now=NOW + timedelta(days=1))
    after = await _stored(db)
    state = after['copy_selection']
    assert state == draw_cards(birth_date=date(2002, 12, 13), today=DAY + timedelta(days=1))
    await _reveal(db, now=NOW + timedelta(days=1), locale='ja')
    assert await _stored(db) == after


@pytest.mark.parametrize("previous_copy_version", ["fortune-copy.v2-day-overview.1", "fortune-copy.v3-editorial.1"])
async def test_copy_snapshot_preserves_today_and_continues_current_cursor(fortune_db, previous_copy_version):
    db = fortune_db
    await _reveal(db)
    state = (await _stored(db))["copy_selection"]
    old_lines = {
        "ko": ["이전에 저장한 애정 해석", "이전에 저장한 애정 제안"],
        "en": ["Previously saved love reading", "Previously saved love suggestion"],
        "ja": ["保存済みの恋愛の解釈", "保存済みの恋愛の提案"],
    }
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        row = await session.get(DailyFortune, db.uid)
        copies = deepcopy(row.copy_by_locale)
        for locale, lines in old_lines.items():
            copies[locale]["categories"]["love"]["text"] = lines
        row.copy_by_locale = copies
        row.copy_version = previous_copy_version
        await session.commit()

    for locale, lines in old_lines.items():
        same_day = await _reveal(db, locale=locale)
        assert same_day["result"]["categories"]["love"]["text"] == lines
        assert same_day["versions"]["copy"] == previous_copy_version
    assert (await _stored(db))["copy_selection"] == state

    tomorrow = await _reveal(db, now=NOW + timedelta(days=1))
    after = await _stored(db)
    selection = after["copy_selection"]
    assert selection["version"] == state["version"] == "fortune-selection.v4"
    assert selection == draw_cards(birth_date=date(2002, 12, 13), today=DAY + timedelta(days=1))
    assert tomorrow["versions"]["copy"] == fortune_catalog.COPY_VERSION
    semantic = {k: v for k, v in after.items() if k != "copy_selection"}
    for locale in old_lines:
        result = await _reveal(db, now=NOW + timedelta(days=1), locale=locale)
        expected = fortune_catalog.load_catalog().render_cards(
            semantic, locale, selection=selection,
        )
        for category, block in expected["categories"].items():
            assert result["result"]["categories"][category]["text"] == block["text"]
    assert (await _stored(db))["copy_selection"] == selection


async def test_real_independent_scores_preserve_old_day_and_switch_next_day(fortune_db, monkeypatch):
    db = fortune_db
    old_response = await _reveal(db)
    old_snapshot = await _stored(db)
    assert old_snapshot["schema_version"] == 4
    monkeypatch.setattr(fortune_scores, "generate_result", _REAL_GENERATE_RESULT)
    assert await _reveal(db) == old_response
    assert await _stored(db) == old_snapshot
    response = await _reveal(db, now=NOW + timedelta(days=1))
    current = await _stored(db)
    assert current["schema_version"] == 4
    assert current["copy_selection"]["version"] == "fortune-selection.v4"
    expected = _REAL_GENERATE_RESULT(birth_date=date(2002, 12, 13), local_date=DAY + timedelta(days=1))
    assert current["overall"] == expected["overall"]
    assert current["categories"] == expected["categories"]
    assert response["result"]["schema_version"] == 3
    assert await _reveal(db, now=NOW + timedelta(days=1)) == response


async def test_profile_edit_preserves_scores_prose_and_color(fortune_db, monkeypatch):
    db = fortune_db
    first = await _reveal(db)
    before = await _stored(db)
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        row = await session.get(DailyFortune, db.uid)
        row.copy_version = "fortune-copy.v5-saved-snapshot.1"
        await session.commit()
    changed = _semantic()
    changed["overall"] = {"score": 90, "reading_code": "overall.d90.general", "expression_route": "overall.d90.general"}
    changed["lucky_color_key"] = fortune_scores.COLORS[9]
    monkeypatch.setattr(fortune_scores, "generate_result", lambda **_: changed)
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        await fortune.put_profile(session, str(db.uid), FortuneProfilePut(
            birth_date=date(2000, 1, 1), gender="woman"), now_utc=NOW)
    after = await _reveal(db)
    assert (await _stored(db))["copy_selection"] == before["copy_selection"]
    assert after["result"] == first["result"]
    assert after["result"]["overall"]["flow"] == first["result"]["overall"]["flow"]
    assert after["result"]["categories"] == first["result"]["categories"]
    assert after["result"]["lucky_color"] == first["result"]["lucky_color"]
    assert after["versions"]["copy"] == "fortune-copy.v5-saved-snapshot.1"


async def test_legacy_profile_edit_preserves_issued_day(fortune_db):
    db = fortune_db
    await _reveal(db)
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        row = await session.get(DailyFortune, db.uid)
        row.semantic_result = _legacy_semantic()
        row.copy_version = "fortune-copy.v2-initial.1"
        await session.commit()
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        await fortune.put_profile(session, str(db.uid), FortuneProfilePut(
            birth_date=date(2000, 1, 1), gender="woman"), now_utc=NOW)
    result = await _reveal(db)
    assert await _stored(db) == _legacy_semantic()
    assert result["versions"]["copy"] == "fortune-copy.v2-initial.1"


async def test_rollback_build_keeps_v4_snapshot_and_labels_new_legacy_copy(fortune_db, monkeypatch):
    db = fortune_db
    first = await _reveal(db)
    initial = await _stored(db)
    monkeypatch.setattr(fortune, "_COPY_POLICY", "legacy")
    assert await _reveal(db) == first
    assert await _stored(db) == initial
    tomorrow = await _reveal(db, now=NOW + timedelta(days=1))
    assert (await _stored(db))["copy_selection"]["version"] == "fortune-selection.v3"
    assert tomorrow["versions"]["copy"] == fortune_catalog.LEGACY_COPY_VERSIONS["ko"]
    monkeypatch.setattr(fortune, "_COPY_POLICY", "tarot")
    assert await _reveal(db, now=NOW + timedelta(days=1)) == tomorrow
    restored = await _reveal(db, now=NOW + timedelta(days=2))
    assert (await _stored(db))["copy_selection"]["version"] == "fortune-selection.v4"
    assert restored["versions"]["copy"] == fortune_catalog.COPY_VERSION


async def test_timezone_changes_and_profile_recreation_keep_date_draw_contract(fortune_db):
    db = fortune_db
    first = await _reveal(db)
    initial = (await _stored(db))["copy_selection"]
    await db.conn.execute("UPDATE profiles SET timezone='Asia/Seoul' WHERE id=$1", db.uid)
    same_date = await _reveal(db)
    assert (await _stored(db))["copy_selection"] == initial
    assert same_date["result"] == first["result"]
    await db.conn.execute("UPDATE profiles SET timezone='Pacific/Kiritimati' WHERE id=$1", db.uid)
    await _reveal(db)
    assert (await _stored(db))["copy_selection"] == draw_cards(birth_date=date(2002, 12, 13), today=DAY + timedelta(days=1))
    await db.conn.execute("UPDATE profiles SET timezone='UTC' WHERE id=$1", db.uid)
    returned = await _reveal(db)
    assert (await _stored(db))["copy_selection"] == initial
    assert returned["result"] == first["result"]
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        await fortune.delete_profile(session, str(db.uid))
    assert not await db.conn.fetchval("SELECT EXISTS(SELECT 1 FROM daily_fortunes WHERE user_id=$1)", db.uid)
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        await fortune.put_profile(session, str(db.uid), FortuneProfilePut(
            birth_date=date(2002, 12, 13), gender="undisclosed"), now_utc=NOW)
    recreated = await _reveal(db)
    assert (await _stored(db))["copy_selection"] == initial
    assert recreated["result"] == first["result"]
