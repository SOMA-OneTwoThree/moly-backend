"""Store periods and daily totals on the existing development database."""
from __future__ import annotations

import os
import uuid
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
import asyncpg
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.profile import Profile
from app.models.subscription import Subscription
from app.services import diary_generation as diaries, llm, privacy, subscription, subscription_periods
from app.services.llm import LLMResult
from db.envfile import assert_dev_target, load_conn
from scripts.subscription_period_starts import PROPOSALS
from scripts import subscription_period_starts
from worker import daily_report

REAL_ACTIVE = privacy.ensure_subject_active
DAY = date(2200, 2, 1)
NOW = datetime(2200, 2, 3, 4, tzinfo=timezone.utc)
CONFIG = {"diary_min_user_chars": 60, "free_launch_until": "2000-01-01T00:00:00+00:00"}


@pytest_asyncio.fixture
async def db(monkeypatch):
    if os.environ.get("MOLY_SUBSCRIPTION_REPORT_TEST_ENV") != "dev":
        pytest.skip("explicit development target required")
    filename = os.environ.get("MOLY_SUBSCRIPTION_REPORT_ENV_FILE", "")
    if not Path(filename).is_absolute() or os.environ.get("MOLY_SCHEMA_TEST_DSN"):
        pytest.fail("absolute existing development env required")
    dsn = load_conn(filename)
    assert_dev_target(filename, dsn)
    engine = create_async_engine(make_url(dsn).set(drivername="postgresql+asyncpg"),
                                 connect_args={"statement_cache_size": 0}, pool_size=1, max_overflow=0)
    monkeypatch.setattr(privacy, "ensure_subject_active", REAL_ACTIVE)
    monkeypatch.setattr(subscription.settings, "revenuecat_api_v2_key", "")
    monkeypatch.setattr(subscription, "_finish_subscription_bonus",
                        AsyncMock(return_value=subscription.HandlerResult(subscription.HANDLED, None)))
    async with engine.connect() as connection:
        outer = await connection.begin()
        sessions = async_sessionmaker(connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
        monkeypatch.setattr(daily_report, "get_sessionmaker", lambda: sessions)
        try:
            yield SimpleNamespace(sessions=sessions, connection=connection)
        finally:
            await outer.rollback()
    await engine.dispose()


async def account(db, language="ko", zone="Asia/Seoul"):
    uid = uuid.uuid4()
    async with db.sessions() as session:
        await session.execute(text("INSERT INTO auth.users(id,created_at) VALUES(:uid,:created)"),
                              {"uid": uid, "created": NOW-timedelta(days=40)})
        await session.execute(text("UPDATE profiles SET language=:lang,timezone=:zone,trial_ends_at=NULL WHERE id=:uid"),
                              {"uid": uid, "lang": language, "zone": zone})
        await session.commit()
    return uid


def event(uid, plan, start, end, *, trial=False, transaction=None, kind="INITIAL_PURCHASE", original=None):
    transaction = transaction or uuid.uuid4().hex
    return {"type": kind, "app_user_id": str(uid), "environment": "PRODUCTION", "store": "APP_STORE",
            "original_transaction_id": original or transaction, "transaction_id": transaction,
            "product_id": f"com.geniusjun.moly.plus.{plan}", "period_type": "TRIAL" if trial else "NORMAL",
            "price_in_purchased_currency": 0 if trial else 10, "currency": "USD",
            "purchased_at_ms": int(start.timestamp()*1000), "expiration_at_ms": int(end.timestamp()*1000),
            "event_timestamp_ms": int(start.timestamp()*1000)}


async def receive(db, payload):
    async with db.sessions() as session:
        return await subscription.ingest_event(session, str(uuid.uuid4()), payload)


@pytest.mark.parametrize("language,zone", [("ko", "Asia/Seoul"), ("ja", "Asia/Tokyo"), ("en", "America/New_York")])
@pytest.mark.parametrize("plan", ["monthly", "yearly"])
@pytest.mark.parametrize("trial", [False, True])
async def test_store_period_to_daily_diary(db, monkeypatch, language, zone, plan, trial):
    uid = await account(db, language, zone)
    boundary = datetime.combine(DAY+timedelta(days=1), datetime.min.time(), ZoneInfo(zone)) + timedelta(hours=4)
    start = boundary.astimezone(timezone.utc)-timedelta(days=2)
    payload = event(uid, plan, start, start+timedelta(days=30), trial=trial)
    assert await receive(db, payload) == subscription.HANDLED
    async with db.sessions() as session:
        row = (await session.execute(select(Subscription).where(Subscription.user_id == uid))).scalar_one()
        assert row.purchased_at == start
        # Nullable imported rows can use the same verified period evidence.
        row.purchased_at = None
        await session.execute(text("INSERT INTO messages(user_id,sender,kind,content,activity_date) VALUES(:uid,'user','normal',:body,:day)"),
                              {"uid": uid, "body": {"ko": "오늘 책을 읽었다. ", "ja": "今日は本を読んだ。", "en": "I read a book today. "}[language]*20, "day": DAY})
        await session.commit()
        profile = await session.get(Profile, uid)
        body = {"ko": "책을 읽었다는 이야기가 기억에 남았다.", "ja": "本の話が心に残った。", "en": "Their story about reading stayed with me."}[language]
        async def generate(system, convo, **kwargs):
            purpose = kwargs["ledger"].purpose
            return LLMResult("OK" if purpose == "diary_self_check" else "Weather: sunny\n"+body, 10, 10)
        monkeypatch.setattr(llm, "generate", generate)
        result = await diaries.generate_for_user(session, profile, DAY, CONFIG, policy=diaries.DiaryPolicy())
        assert result["created"] and result["source"] == "llm"
        count = await session.scalar(text("SELECT count(*) FROM diary_claim_sources WHERE user_id=:uid"), {"uid": uid})
        assert count == 1
        await session.refresh(profile)
        again = await diaries.generate_for_user(session, profile, DAY, CONFIG, policy=diaries.DiaryPolicy())
        assert again["skipped"]


async def test_renewal_period_boundary_and_gap(db):
    uid = await account(db)
    first = NOW-timedelta(days=10)
    payload = event(uid, "yearly", first, first+timedelta(days=2), trial=True)
    assert await receive(db, payload) == subscription.HANDLED
    renewed = event(uid, "yearly", NOW-timedelta(days=2), NOW+timedelta(days=30),
                    kind="RENEWAL", original=payload["original_transaction_id"])
    assert await receive(db, renewed) == subscription.HANDLED
    async with db.sessions() as session:
        current = await subscription_periods.subscription_at(session, uid, NOW)
        assert current and current.purchased_at == NOW-timedelta(days=2)
        assert await subscription_periods.subscription_at(session, uid, first+timedelta(hours=2))
        assert await subscription_periods.subscription_at(session, uid, NOW-timedelta(days=5)) is None


async def test_unresolved_period_keeps_generation_uncommitted(db, monkeypatch):
    uid = await account(db)
    async with db.sessions() as session:
        await session.execute(text("""INSERT INTO subscriptions(user_id,plan,status,original_transaction_id,
                              expires_at,environment) VALUES(:uid,'yearly','active',:tx,:end,'PRODUCTION')"""),
                              {"uid": uid, "tx": uuid.uuid4().hex, "end": NOW+timedelta(days=30)})
        await session.execute(text("INSERT INTO messages(user_id,sender,content,activity_date) VALUES(:uid,'user',:body,:day)"),
                              {"uid": uid, "body": "오늘 책을 읽고 잠깐 산책을 했어. "*10, "day": DAY})
        await session.commit()
        profile = await session.get(Profile, uid)
        provider = AsyncMock(side_effect=AssertionError("unexpected model call"))
        monkeypatch.setattr(diaries, "_personal", provider)
        with pytest.raises(ValueError, match="subscription period unavailable"):
            await diaries.generate_for_user(session, profile, DAY, CONFIG, policy=diaries.DiaryPolicy())
        provider.assert_not_awaited()
        assert await session.scalar(text("SELECT count(*) FROM diaries WHERE user_id=:uid"), {"uid": uid}) == 0
        assert await session.scalar(text("SELECT count(*) FROM diary_generation_results WHERE user_id=:uid"), {"uid": uid}) == 0


async def test_report_conversion_and_continuing_period_are_distinct(db):
    config = {"diary_min_user_chars": 60, "launch_until": None}
    before = await daily_report.business(NOW, config)
    uid = await account(db)
    start, end = NOW-timedelta(days=10), NOW-timedelta(days=1)
    initial = event(uid, "yearly", start, end, trial=True)
    assert await receive(db, initial) == subscription.HANDLED
    renewed = event(uid, "yearly", end, NOW+timedelta(days=30), kind="RENEWAL",
                    original=initial["original_transaction_id"])
    renewed["is_trial_conversion"] = True
    assert await receive(db, renewed) == subscription.HANDLED
    assert await receive(db, renewed) == subscription.HANDLED
    expiration = {**initial, "type": "EXPIRATION", "event_timestamp_ms": int(end.timestamp()*1000)}
    assert await receive(db, expiration) == subscription.NO_OP
    after = await daily_report.business(NOW, config)
    assert after["subscriptions"]["paid"] == before["subscriptions"]["paid"]+1
    assert after["subscriptions"]["converted"] == before["subscriptions"]["converted"]+1
    assert after["subscriptions"]["ended"] == before["subscriptions"]["ended"]


async def test_period_start_proposal_matches_latest_transaction(db):
    uid = await account(db)
    payload = event(uid, "monthly", NOW-timedelta(days=1), NOW+timedelta(days=30))
    assert await receive(db, payload) == subscription.HANDLED
    async with db.sessions() as session:
        row = (await session.execute(select(Subscription).where(Subscription.user_id == uid))).scalar_one()
        row.purchased_at = None
        await session.flush()
        query = text(PROPOSALS.replace("$1::uuid", "CAST(:identity AS uuid)").replace("$1", ":identity"))
        proposal = (await session.execute(query, {"identity": row.id})).mappings().one()
        assert proposal["period_start"] == NOW-timedelta(days=1)
        await session.execute(text("UPDATE subscriptions SET purchased_at=:start WHERE id=:identity AND purchased_at IS NULL"),
                              {"start": proposal["period_start"], "identity": row.id})
        assert not (await session.execute(query, {"identity": row.id})).all()


async def test_report_counts_and_independent_diary_target(db):
    config = {"diary_min_user_chars": 60, "launch_until": datetime(2000, 1, 1, tzinfo=timezone.utc)}
    before = await daily_report.business(NOW, config)
    assert all(value is not None for value in before.values()), before
    uid = await account(db, "en", "UTC")
    payload = event(uid, "yearly", NOW-timedelta(days=10), NOW+timedelta(days=10), trial=True)
    assert await receive(db, payload) == subscription.HANDLED
    async with db.sessions() as session:
        await session.execute(text("UPDATE subscriptions SET purchased_at=NULL WHERE user_id=:uid"), {"uid": uid})
        await session.execute(text("INSERT INTO messages(user_id,sender,content,activity_date,created_at) VALUES(:uid,'user',:body,:day,:created)"),
                              {"uid": uid, "body": "I read a book today. "*20, "day": DAY, "created": NOW-timedelta(hours=36)})
        await session.execute(text("INSERT INTO diary_generation_results(user_id,target_date,status) VALUES(:uid,:day,'no_entry')"),
                              {"uid": uid, "day": DAY})
        await session.commit()
    after = await daily_report.business(NOW, config)
    assert all(value is not None for value in after.values()), after
    assert after["activity"]["dau"] == before["activity"]["dau"]+1
    assert after["activity"]["mau"] == before["activity"]["mau"]+1
    assert after["subscriptions"]["trial"] == before["subscriptions"]["trial"]+1
    assert after["diaries"]["expected"] == before["diaries"]["expected"]+1
    assert after["diaries"]["completed"] == before["diaries"]["completed"]


async def test_daily_counts_and_subscription_comparison(db):
    await daily_report.record_tick(NOW-timedelta(days=1), {"diaries": 2, "push_sent": 5})
    await daily_report.record_tick(NOW-timedelta(days=1), {"diaries": 2, "push_sent": 5})
    async with db.sessions() as session:
        from app.models.app_config import AppConfig
        value = (await session.get(AppConfig, daily_report.TOTALS_KEY)).value
        assert value["days"]["2200-02-02"]["diaries"] == 2
    await daily_report.subscription_change(NOW-timedelta(days=1), {"total": 3})
    assert await daily_report.subscription_change(NOW, {"total": 5}) == 2


async def test_period_start_tool_preview_lock_and_apply(db):
    filename = os.environ["MOLY_SUBSCRIPTION_REPORT_ENV_FILE"]
    dsn = load_conn(filename)
    assert_dev_target(filename, dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=8, command_timeout=10)
    uid, sid, eid = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    tx = uuid.uuid4().hex
    created = False
    args = SimpleNamespace(env=filename, commit=False, allow_prod=False, expected_sha256=None)
    try:
        baseline = await subscription_period_starts.run(args)
        if baseline["candidates"]:
            pytest.skip("development period candidates already present")
        await conn.execute("INSERT INTO auth.users(id,created_at) VALUES($1,$2)", uid, NOW-timedelta(days=40))
        created = True
        payload = event(uid, "yearly", NOW-timedelta(days=1), NOW+timedelta(days=30), transaction=tx, trial=True)
        await conn.execute("""INSERT INTO subscriptions(id,user_id,plan,status,original_transaction_id,
                           latest_transaction_id,environment,expires_at)
                           VALUES($1,$2,'yearly','active',$3,$3,'PRODUCTION',$4)""", sid, uid, tx, NOW+timedelta(days=30))
        await conn.execute("INSERT INTO revenuecat_events(event_id,payload,status) VALUES($1,$2::jsonb,'processed')",
                           eid, json.dumps(payload))
        preview = await subscription_period_starts.run(args)
        assert preview["candidates"] == 1 and preview["updated"] == 0
        args.commit = True
        args.expected_sha256 = "invalid"
        with pytest.raises(ValueError, match="SHA-256"):
            await subscription_period_starts.run(args)
        args.expected_sha256 = preview["sha256"]
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(uid))
            skipped = await subscription_period_starts.run(args)
            assert skipped["updated"] == 0 and skipped["skipped"] == 1
        async with conn.transaction():
            await conn.execute("SELECT id FROM subscriptions WHERE id=$1 FOR UPDATE", sid)
            skipped = await subscription_period_starts.run(args)
            assert skipped["updated"] == 0 and skipped["skipped"] == 1
        applied = await subscription_period_starts.run(args)
        assert applied["updated"] == 1 and applied["skipped"] == 0
        assert await conn.fetchval("SELECT purchased_at FROM subscriptions WHERE id=$1", sid) == NOW-timedelta(days=1)
        args.commit = False
        assert (await subscription_period_starts.run(args))["candidates"] == 0
    finally:
        await conn.execute("DELETE FROM revenuecat_events WHERE event_id=$1", eid)
        if created:
            await conn.execute("DELETE FROM privacy_ledger_events WHERE user_id=$1", uid)
            await conn.execute("DELETE FROM auth.users WHERE id=$1", uid)
            await conn.execute("DELETE FROM privacy_subject_barriers WHERE user_id=$1", uid)
        await conn.close()


@pytest.mark.parametrize("launch", [None, datetime(2000, 1, 1, tzinfo=timezone.utc)])
async def test_daily_message_is_rendered_from_database_without_sending(db, monkeypatch, launch):
    from app.services import config_store, monitoring_state
    from worker import monitoring
    monkeypatch.setattr(monitoring, "get_sessionmaker", lambda: db.sessions)
    async with db.sessions() as session:
        keys = [config_store.WORKER_LAST_SUCCESS_KEY, *(config_store.RETENTION_LAST_SUCCESS_PREFIX+name
                for name in ("retention_idempotency_gc", "usage_ledger_rollup", "retention_jobs_gc", "mem0_candidate_gc"))]
        for key in keys:
            await config_store.set_config_value(session, key, NOW.isoformat())
        await config_store.set_config_value(session, "free_launch_until", launch.isoformat() if launch else None)
        await config_store.set_config_value(session, config_store.WORKER_TZ_UNRESOLVABLE_KEY, [])
        for signal in ("worker_diary", "worker_memory", "worker_timeout", "worker_push", "rc_processor"):
            await config_store.set_config_value(session, monitoring_state.key_for(signal), {"active": False})
    rc = {env: dict(failed=0, pending=0, provider_unavailable=0) for env in ("PRODUCTION", "SANDBOX", "UNKNOWN")}
    message = await monitoring._daily_message(NOW, rc, {})
    assert "DAU:" in message and "MAU:" in message and "현재 구독 이용자:" in message
    assert "집계 확인 필요" not in message
    assert "RC PRODUCTION" not in message and "provider" not in message
