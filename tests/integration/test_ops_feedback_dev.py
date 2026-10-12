"""개발 Supabase 전용. 합성 자료만 사용하고 모든 쓰기/임시 테이블을 rollback한다.

MOLY_OPS_TEST_ENV=dev MOLY_OPS_ENV_FILE=/path/to/development/.env 로 명시 실행.
Slack/provider는 호출하지 않으며 공용 설정·실제 사용자 행을 변경하지 않는다.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.services import jobs, monitoring_state as state, usage_ledger, subscription
from db.envfile import assert_dev_target, load_conn
from worker import monitoring

NOW = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def engine():
    if os.environ.get("MOLY_OPS_TEST_ENV") != "dev":
        pytest.skip("explicit development database gate required")
    path = os.environ.get("MOLY_OPS_ENV_FILE", "dev")
    dsn = load_conn(path)
    assert_dev_target(path, dsn)
    engine = create_async_engine(make_url(dsn).set(drivername="postgresql+asyncpg"),
                                 connect_args={"statement_cache_size": 0, "command_timeout": 5},
                                 hide_parameters=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db(engine, monkeypatch):
    namespace = "test_ops_" + uuid.uuid4().hex
    monkeypatch.setattr(state, "environment", lambda: namespace)
    async with engine.connect() as conn:
        tx = await conn.begin()
        try:
            async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
                yield session
        finally:
            await tx.rollback()
    async with engine.connect() as conn:
        remaining = (await conn.execute(text("SELECT count(*) FROM app_config WHERE key LIKE :prefix"),
                                        {"prefix": f"monitoring:alert:{namespace}:%"})).scalar_one()
        assert remaining == 0


def incident(active=True, fingerprint="synthetic-a"):
    return state.Observation("rc_production", active, fingerprint, "synthetic incident")


async def test_claim_failure_retry_cas_recovery_and_daily_once(db):
    claim = await state._claim(db, incident(), NOW)
    await db.flush()
    assert claim is not None
    assert await state._claim(db, incident(), NOW + timedelta(seconds=1)) is None
    assert await state._ack(db, claim, sent=False, now=NOW)
    await db.flush()
    assert await state._claim(db, incident(), NOW + timedelta(seconds=59)) is None
    replacement = await state._claim(db, incident(), NOW + timedelta(seconds=60))
    await db.flush()
    assert replacement is not None and replacement.token != claim.token
    assert not await state._ack(db, claim, sent=True, now=NOW + timedelta(seconds=61))
    assert await state._ack(db, replacement, sent=True, now=NOW + timedelta(seconds=61))
    await db.flush()
    assert await state._claim(db, incident(fingerprint="synthetic-b"), NOW + timedelta(minutes=15)) is None
    updated = await state._claim(db, incident(fingerprint="synthetic-b"), NOW + timedelta(hours=1, minutes=2))
    await db.flush()
    assert updated is not None
    # 네트워크 전송 중 정상 관측이 들어와도 이전 sender의 ack가 관측을 덮지 않는다.
    assert await state._claim(db, incident(False), NOW + timedelta(hours=1, minutes=2, seconds=1)) is None
    await db.flush()
    assert await state._ack(db, updated, sent=True, now=NOW + timedelta(hours=1, minutes=2, seconds=2))
    recovered = await state._claim(db, incident(False), NOW + timedelta(hours=1, minutes=3))
    await db.flush()
    assert recovered is not None and not recovered.active
    await state._ack(db, recovered, sent=True, now=NOW + timedelta(hours=1, minutes=3))
    await db.flush()
    assert await state._claim(db, incident(False), NOW + timedelta(hours=2)) is None
    daily = state.Observation("daily_status", True, "2026-10-12", "synthetic", daily=True, severity="status")
    first = await state._claim(db, daily, NOW)
    await db.flush()
    assert first is not None
    await state._ack(db, first, sent=True, now=NOW)
    await db.flush()
    assert await state._claim(db, daily, NOW + timedelta(hours=1)) is None


async def test_concurrent_first_claim_is_serialized_and_rollback_cleans(engine, monkeypatch):
    namespace = "test_ops_" + uuid.uuid4().hex
    monkeypatch.setattr(state, "environment", lambda: namespace)
    async with engine.connect() as first, engine.connect() as second:
        tx1, tx2 = await first.begin(), await second.begin()
        try:
            async with AsyncSession(bind=first) as s1, AsyncSession(bind=second) as s2:
                assert await state._claim(s1, incident(), NOW) is not None
                await s1.flush()
                # 2번째 INSERT ON CONFLICT가 첫 트랜잭션을 기다린다. 실제 전송은 하지 않는다.
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(state._claim(s2, incident(), NOW), timeout=0.15)
        finally:
            if tx2.is_active:
                await tx2.rollback()
            if tx1.is_active:
                await tx1.rollback()
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM app_config WHERE key LIKE :p"),
                                   {"p": f"monitoring:alert:{namespace}:%"})).scalar_one() == 0


async def test_rc_grouping_same_count_new_event_and_no_raw_payload(db):
    await db.execute(text("CREATE TEMP TABLE revenuecat_events (LIKE public.revenuecat_events INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP"))
    await db.execute(text("""
      INSERT INTO revenuecat_events(event_id,payload,status,received_at) VALUES
      ('synthetic-a','{"environment":"sandbox","app_user_id":"unread-private"}','failed',:old),
      ('synthetic-b','{"environment":"PRODUCTION"}','pending',:old),
      ('synthetic-c','{"environment":"unrecognized"}','failed',:old),
      ('synthetic-recent','{"environment":"PRODUCTION"}','pending',:now),
      ('synthetic-resolved','{"environment":"PRODUCTION"}','processed',:old)
    """), {"old": NOW - timedelta(hours=2), "now": NOW})
    snapshot = await monitoring.rc_snapshot(db, NOW)
    assert snapshot["SANDBOX"]["failed"] == 1 and snapshot["PRODUCTION"]["pending"] == 1
    assert snapshot["UNKNOWN"]["failed"] == 1
    assert "unread-private" not in str(snapshot)
    previous = snapshot["SANDBOX"]["fingerprint"]
    await db.execute(text("UPDATE revenuecat_events SET event_id='synthetic-new' WHERE event_id='synthetic-a'"))
    changed = await monitoring.rc_snapshot(db, NOW)
    assert changed["SANDBOX"]["failed"] == 1 and changed["SANDBOX"]["fingerprint"] != previous
    await db.execute(text("UPDATE revenuecat_events SET next_attempt_at=now() WHERE event_id='synthetic-new'"))
    assert (await monitoring.rc_snapshot(db, NOW))["SANDBOX"]["fingerprint"] == changed["SANDBOX"]["fingerprint"]
    await db.execute(text("""INSERT INTO revenuecat_events(event_id,payload,status,received_at,last_error) VALUES
      ('synthetic-provider','{"environment":"SANDBOX"}','pending',:now,'held:provider_history_unavailable'),
      ('synthetic-business','{"environment":"SANDBOX"}','pending',:now,'held:provider_owner_changed')"""),
                     {"now": NOW})
    newest = await monitoring.rc_snapshot(db, NOW)
    assert newest["SANDBOX"]["provider_unavailable"] == 1
    assert newest["SANDBOX"]["pending"] == 0  # 새 pending은 장기 backlog로 오표기하지 않는다.


async def test_cost_nullable_and_kst_boundary_sql(db):
    await db.execute(text("CREATE TEMP TABLE ai_usage_ledger (LIKE public.ai_usage_ledger INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP"))
    start, end = monitoring.cost_window(NOW)
    for offset, status, cost, experiment in [
        (0, "completed", 100, None), (1, "unknown_usage", None, None),
        (2, "failed", None, "synthetic-experiment"), (-1, "completed", 999, None),
        (86400, "completed", 999, None),
    ]:
        await db.execute(text("""INSERT INTO ai_usage_ledger
          (call_id,lane,purpose,provider,model,started_at,status,cost_micro_usd,experiment_id,price_catalog_version)
          VALUES(:id,'background','feedback_translate','openai','synthetic-model',:at,:status,:cost,:exp,:catalog)"""),
          {"id": uuid.uuid4(), "at": start + timedelta(seconds=offset), "status": status,
           "cost": cost, "exp": experiment, "catalog": 1 if status == "completed" else None})
    row = (await db.execute(monitoring._COST_SQL, {"start": start, "end": end})).mappings().one()
    assert row["calls"] == 3 and row["cost"] == 100 and row["unknown"] == 2 and row["experiments"] == 1
    # feedback purpose 허용 + 카탈로그 없는 모델은 0원 completed로 만들지 않는다.
    call_id = await usage_ledger.begin(db, lane="background", purpose="feedback_translate",
                                       provider="openai", model="synthetic-unpriced")
    await usage_ledger.complete(db, call_id, price=None, input_tokens=10, output_tokens=2)
    priced = (await db.execute(text("SELECT status,cost_micro_usd FROM ai_usage_ledger WHERE call_id=:id"),
                               {"id": call_id})).one()
    assert priced.status == "unknown_usage" and priced.cost_micro_usd is None


async def test_queue_excludes_every_successful_replay_ancestor(db):
    await db.execute(text("CREATE TEMP TABLE async_jobs (LIKE public.async_jobs INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP"))
    a, b, c, unresolved = [uuid.uuid4() for _ in range(4)]
    for ident, previous, status in [(a, None, "dead"), (b, a, "dead"), (c, b, "succeeded"), (unresolved, None, "dead")]:
        await db.execute(text("""INSERT INTO async_jobs(id,queue,job_type,payload,dedup_key,state,replay_of,max_attempts)
          VALUES(:id,'memory','synthetic','{}',:key,:state,:previous,3)"""),
                         {"id": ident, "key": str(ident), "state": status, "previous": previous})
    snapshot = await jobs.queue_stats(db)
    assert snapshot["memory"]["dead"] == 1


async def test_lifetime_bonus_sql_is_scoped_and_revocation_does_not_regrant(db, monkeypatch):
    await db.execute(text("CREATE TEMP TABLE subscription_hay_grants (LIKE public.subscription_hay_grants INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP"))
    uid, other = uuid.uuid4(), uuid.uuid4()
    for user_id, plan in [(uid, "monthly"), (other, "yearly")]:
        await db.execute(text("""INSERT INTO subscription_hay_grants(id,user_id,plan,revoked_at)
          VALUES(:id,:uid,:plan,:at)"""), {"id": uuid.uuid4(), "uid": user_id, "plan": plan, "at": NOW})
    assert await subscription._grant_exists(db, uid, "monthly")
    assert not await subscription._grant_exists(db, uid, "yearly")
    review = AsyncMock(side_effect=AssertionError("no external provider"))
    ledger = AsyncMock(side_effect=AssertionError("no financial mutation"))
    monkeypatch.setattr(subscription, "_bonus_review", review)
    monkeypatch.setattr(subscription.hay_ledger, "apply", ledger)
    pay = SimpleNamespace(user_id=uid, subscription_plan="monthly", subscription_hay_grant_id=None,
                          subscription_bonus_review="held:provider_history_unavailable")
    result = await subscription._finish_subscription_bonus(db, SimpleNamespace(user_id=uid), pay, {}, "monthly")
    assert result.outcome == subscription.HANDLED and pay.subscription_hay_grant_id is None
    assert pay.subscription_bonus_review == "not_granted:user_plan_previously_granted"
    review.assert_not_awaited()
    ledger.assert_not_awaited()
    assert (await db.execute(text("SELECT count(*) FROM subscription_hay_grants WHERE revoked_at IS NOT NULL"))).scalar_one() == 2
