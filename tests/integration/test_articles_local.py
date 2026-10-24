"""Real transactions: reward replay, competing requests, rollback and account isolation."""
import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import articles, hay_ledger
from db.schema_contract import require_scratch

@pytest_asyncio.fixture
async def engine():
    dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('MOLY_SCHEMA_TEST_DSN required')
    require_scratch(dsn)
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'), poolclass=NullPool)
    yield engine
    await engine.dispose()

@pytest_asyncio.fixture
async def users(engine):
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    async with engine.begin() as conn:
        for uid in ids:
            await conn.execute(text('INSERT INTO auth.users(id) VALUES (:u)'), {'u': uuid.UUID(uid)})
    yield ids
    async with engine.begin() as conn:
        for uid in ids:
            await conn.execute(text('DELETE FROM auth.users WHERE id=:u'), {'u': uuid.UUID(uid)})

async def request(engine, uid, article='sample-1', **kwargs):
    async with AsyncSession(engine, expire_on_commit=False) as db:
        return await articles.record(db, uid, article, **kwargs)

@pytest.mark.parametrize("article_id", ["sample-1", "sample-6", "sample-7"])
async def test_feedback_reward_and_replay_are_independent(engine, users, article_id):
    uid, other = users
    async with AsyncSession(engine) as db:
        assert not any(a['is_read'] for a in (await articles.list_activity(db, uid))['articles'])
    feedback = await request(engine, uid, article_id, feedback='not_helpful')
    assert feedback['is_read'] and not feedback['reward_claimed'] and feedback['granted'] == 0
    reward = await request(engine, uid, article_id)
    assert reward['granted'] == 20 and reward['balance_after'] == 20
    assert reward['feedback'] == 'not_helpful'
    replay = await request(engine, uid, article_id)
    assert replay['granted'] == 0 and replay['balance_after'] == 20
    updated = await request(engine, uid, article_id, feedback='helpful')
    assert updated['reward_claimed'] and updated['feedback'] == 'helpful'
    assert (await request(engine, other, article_id))['granted'] == 20
    assert (await request(engine, uid, 'sample-2'))['balance_after'] == 40

async def test_concurrent_rewards_and_feedback_do_not_double_pay(engine, users):
    uid = users[0]
    result = await asyncio.gather(*[request(engine, uid) for _ in range(8)], request(engine, uid, feedback='helpful'))
    assert sum(r['granted'] for r in result) == 20
    async with AsyncSession(engine) as db:
        assert await db.scalar(text("SELECT count(*) FROM hay_transactions WHERE user_id=:u AND type='article_reward'"), {'u':uuid.UUID(uid)}) == 1
        activity = (await articles.list_activity(db, uid))['articles'][0]
        assert activity['is_read'] and activity['reward_claimed'] and activity['feedback']=='helpful'

async def test_ledger_failure_rolls_back_receipt_and_balance(engine, users, monkeypatch):
    original = hay_ledger.apply
    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('simulate failure before commit')
    with monkeypatch.context() as patch:
        patch.setattr(hay_ledger, 'apply', fail)
        with pytest.raises(RuntimeError):
            await request(engine, users[0])
    async with AsyncSession(engine) as db:
        assert not (await articles.list_activity(db, users[0]))['articles'][0]['is_read']
    assert (await request(engine, users[0]))['balance_after'] == 20

async def test_unknown_article_does_not_grant(engine, users):
    from app.core.errors import AppError
    with pytest.raises(AppError):
        await request(engine, users[0], 'sample-999')
