"""탈퇴 계정 결제 판정을 실제 dev Postgres에서 확인한다. 합성 행은 롤백되는 트랜잭션 안에만 있다.

실행: MOLY_CHECKPOINT_TEST_ENV=dev MOLY_CHECKPOINT_ENV_FILE=<dev env 절대경로> pytest -q 이 파일
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.services import subscription_processing as processing
from db.envfile import assert_dev_target, load_conn

ANON = "$RCAnonymousID:00000000000000000000000000000000"


@pytest_asyncio.fixture
async def dev_session():
    if os.environ.get("MOLY_CHECKPOINT_TEST_ENV") != "dev":
        pytest.skip("MOLY_CHECKPOINT_TEST_ENV=dev required")
    env_file = os.environ.get("MOLY_CHECKPOINT_ENV_FILE", ".env")
    dsn = load_conn(env_file)
    assert_dev_target(env_file, dsn)
    engine = create_async_engine(
        make_url(dsn).set(drivername="postgresql+asyncpg"), echo=False, hide_parameters=True,
        connect_args={"statement_cache_size": 0, "command_timeout": 15},
    )
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
                await conn.execute(text("SET LOCAL statement_timeout = '10s'"))
                async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
                    yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def _event(session, owner, original, tx, kind, **names):
    payload = {"type": kind, "app_user_id": str(owner), "transaction_id": tx,
               "original_transaction_id": original, "is_family_share": False,
               "period_type": "TRIAL" if kind == "INITIAL_PURCHASE" else "NORMAL",
               "price_in_purchased_currency": 0 if kind == "INITIAL_PURCHASE" else 59000,
               "environment": "PRODUCTION", "store": "APP_STORE",
               "original_app_user_id": ANON, "aliases": [str(owner), ANON]}
    payload.update(names)
    await session.execute(text(
        "INSERT INTO revenuecat_events(event_id, payload, status) VALUES (:id, CAST(:p AS jsonb), 'processed')"
    ), {"id": f"itest-{uuid.uuid4()}", "p": json.dumps(payload)})
    return payload


async def _setup(session, state):
    owner, original, tx = uuid.uuid4(), f"itest-otx-{uuid.uuid4()}", f"itest-tx-{uuid.uuid4()}"
    await session.execute(text(
        "INSERT INTO privacy_subject_barriers(user_id, state, operation_id) VALUES (:u, :s, :o)"
    ), {"u": owner, "s": state, "o": uuid.uuid4()})
    await _event(session, owner, original, original, "INITIAL_PURCHASE")
    renewal = await _event(session, owner, original, tx, "RENEWAL")
    target = processing.Target(None, {"id": f"itest-object-{uuid.uuid4()}", "customer_id": str(owner)},
                               [{"id": original, "purchased_at": 1}, {"id": tx, "purchased_at": 2}], {})
    prepared = processing.Prepared([], None, datetime.now(timezone.utc), targets=[target])
    return owner, original, renewal, prepared


async def test_deleted_account_purchase_is_judged_on_the_real_schema(dev_session):
    owner, original, renewal, prepared = await _setup(dev_session, "deleted")
    assert await processing._deleted_owner_purchase(dev_session, renewal, prepared) == processing._DELETED


@pytest.mark.parametrize("names", [
    {"original_app_user_id": {"id": "x"}},          # JSON 객체
    {"original_app_user_id": 7},                     # 숫자
    {"aliases": None, "original_app_user_id": None, "app_user_id": None},   # 이름 없음
    {"aliases": ["OWNER", {"id": "x"}]},            # 별칭에 객체
    {"aliases": "OWNER"},                            # 별칭이 목록이 아님
    {"aliases": ["OWNER", "OTHER_UUID"]},            # 다른 계정 별칭
])
async def test_odd_or_foreign_names_in_a_past_event_block_on_the_real_schema(dev_session, names):
    owner, original, renewal, prepared = await _setup(dev_session, "deleted")
    other = str(uuid.uuid4())
    def fill(value):
        if isinstance(value, list):
            return [fill(item) for item in value]
        return str(owner) if value == "OWNER" else other if value == "OTHER_UUID" else value
    await _event(dev_session, owner, original, f"itest-tx-{uuid.uuid4()}", "RENEWAL",
                 **{key: fill(value) for key, value in names.items()})
    assert await processing._deleted_owner_purchase(dev_session, renewal, prepared) is None


async def test_deletion_in_progress_and_live_profile_on_the_real_schema(dev_session):
    _, _, renewal, prepared = await _setup(dev_session, "deleting")
    assert await processing._deleted_owner_purchase(dev_session, renewal, prepared) == processing._DELETING
    owner, _, renewal, prepared = await _setup(dev_session, "deleted")
    await dev_session.execute(text("INSERT INTO auth.users(id, created_at) VALUES (:u, now())"), {"u": owner})
    await dev_session.execute(text("INSERT INTO profiles(id) VALUES (:u) ON CONFLICT DO NOTHING"), {"u": owner})
    assert await processing._deleted_owner_purchase(dev_session, renewal, prepared) is None
