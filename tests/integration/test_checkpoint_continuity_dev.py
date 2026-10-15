"""Existing dev Supabase only; synthetic rows live inside rolled-back transactions."""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import AppError
from app.services import chat, checkpoint, checkpoint_repo, chat_turns
from app.services.chat_turns import verify_publish as verify_publish_actual, finish_publish as finish_publish_actual
from db.envfile import assert_dev_target, load_conn


@pytest_asyncio.fixture
async def checkpoint_db():
    if os.environ.get("MOLY_CHECKPOINT_TEST_ENV") != "dev":
        pytest.skip("MOLY_CHECKPOINT_TEST_ENV=dev required")
    env_file = os.environ.get("MOLY_CHECKPOINT_ENV_FILE", ".env")
    dsn = load_conn(env_file)
    assert_dev_target(env_file, dsn)
    engine = create_async_engine(
        make_url(dsn).set(drivername="postgresql+asyncpg"), echo=False, hide_parameters=True,
        connect_args={"statement_cache_size": 0, "command_timeout": 15},
    )
    users = [uuid.uuid4(), uuid.uuid4()]
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
                await conn.execute(text("SET LOCAL statement_timeout = '10s'"))
                for uid in users:
                    await conn.execute(text("INSERT INTO auth.users(id,created_at) VALUES(:u,now())"), {"u": uid})
                    await conn.execute(text("INSERT INTO profiles(id) VALUES(:u) ON CONFLICT DO NOTHING"), {"u": uid})
                    await conn.execute(text("INSERT INTO chat_contexts(user_id) VALUES(:u)"), {"u": uid})
                async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
                    yield session, users
            finally:
                await transaction.rollback()
        async with engine.connect() as conn:
            count = (await conn.execute(text("SELECT count(*) FROM auth.users WHERE id IN (:a,:b)"),
                                        {"a": users[0], "b": users[1]})).scalar_one()
            assert count == 0
    finally:
        await engine.dispose()


async def insert_job(session, uid, *, state="ready", generation=0):
    job_id = uuid.uuid4()
    await session.execute(text("""
      INSERT INTO async_jobs(id,queue,job_type,user_id,dedup_key,payload,state,max_attempts,
                             lease_owner,lease_token,lease_until)
      VALUES(:j,'content','conversation_checkpoint',:u,:dedup,
             jsonb_build_object('memory_generation',CAST(:g AS bigint)),:s,3,
             CASE WHEN :s='running' THEN 'synthetic-test' END,
             CASE WHEN :s='running' THEN CAST(:token AS uuid) END,
             CASE WHEN :s='running' THEN now()+interval '1 minute' END)
    """), {"j": job_id, "u": uid, "dedup": str(uuid.uuid4()), "g": generation,
             "s": state, "token": uuid.uuid4()})
    return job_id


async def test_pending_filters_current_generation_state_and_tenant(checkpoint_db):
    session, (mine, other) = checkpoint_db
    for state in ("ready", "running", "dead", "cancelled", "succeeded"):
        for generation in (0, 1):
            async with session.begin_nested() as savepoint:
                await insert_job(session, mine, state=state, generation=generation)
                await insert_job(session, other)
                pending = (await session.execute(checkpoint_repo._PENDING_SQL, {"user_id": mine})).first()
                assert (pending is not None) == (state in {"ready", "running"} and generation == 0)
                await savepoint.rollback()


async def test_published_coverage_is_monotone_current_generation_and_tenant_scoped(checkpoint_db):
    session, (mine, other) = checkpoint_db
    boundaries = []
    for uid in (mine, mine, other):
        boundaries.append((await session.execute(text("""
          INSERT INTO messages(user_id,sender,kind,content,activity_date)
          VALUES(:u,'user','normal','Synthetic source',CURRENT_DATE) RETURNING id
        """), {"u": uid})).scalar_one())
    low, high, foreign = boundaries
    for uid, through, version, digest in (
        (mine, high, checkpoint.SUMMARIZER_VERSION, "a"),
        (mine, low, checkpoint.LEGACY_SUMMARIZER_VERSION, "b"),
        (other, foreign, checkpoint.SUMMARIZER_VERSION, "c"),
    ):
        await checkpoint_repo.insert(session, user_id=uid, through_message_id=through,
                                     summary="Synthetic checkpoint", source_hash=digest * 64,
                                     expected_generation=0, version=version)
    latest = await checkpoint_repo.load_latest(session, mine)
    assert latest.through_message_id == high
    await session.execute(text("UPDATE chat_contexts SET memory_generation=1 WHERE user_id=:u"), {"u": mine})
    assert await checkpoint_repo.load_latest(session, mine) is None
    assert await checkpoint_repo.insert(session, user_id=mine, through_message_id=60,
                                        summary="Synthetic late checkpoint", source_hash="a" * 64,
                                        expected_generation=0) is None


async def test_phase2_generation_guard_and_anchor_job_rollback(checkpoint_db):
    from datetime import datetime, timezone
    session, (uid, _) = checkpoint_db
    token = uuid.uuid4()
    lease = chat_turns.Lease(token, 1, 0)
    await session.execute(text("""
      INSERT INTO chat_active_turns(user_id,turn_seq,idempotency_key,request_hash,
                                    base_context_revision,lease_token,lease_until)
      VALUES(:u,1,'synthetic-checkpoint-test','synthetic',0,:t,now()+interval '1 minute')
    """), {"u": uid, "t": token})
    await verify_publish_actual(session, user_id=uid, lease=lease, memory_generation=0)
    await session.execute(text("UPDATE chat_contexts SET memory_generation=1 WHERE user_id=:u"), {"u": uid})
    with pytest.raises(AppError):
        await verify_publish_actual(session, user_id=uid, lease=lease, memory_generation=0)
    with pytest.raises(AppError):
        async with session.begin_nested():
            await chat._save_anchor(session, uid, 21)
            await insert_job(session, uid, generation=1)
            stale = chat_turns.Lease(token, 1, 99)
            await finish_publish_actual(session, user_id=uid, lease=stale, now=datetime.now(timezone.utc))
    anchor = (await session.execute(text("SELECT anchor_message_id FROM chat_contexts WHERE user_id=:u"),
                                    {"u": uid})).scalar_one()
    jobs = (await session.execute(text("SELECT count(*) FROM async_jobs WHERE user_id=:u"),
                                  {"u": uid})).scalar_one()
    assert anchor == 0
    assert jobs == 0
