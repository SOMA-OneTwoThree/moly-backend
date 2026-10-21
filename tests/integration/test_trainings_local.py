"""Training service flow on a disposable local DB: ORM mapping, CHECK constraints and the hay ledger.

Service commits become flushes so the whole flow shares one transaction that is rolled back.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.schemas.trainings import TrainingAnswerRequest, TrainingClaimRequest
from app.services import training_catalog, training_grading, trainings
from app.services.training_catalog import TrainingCatalog
from app.services.training_grading import Verdict
from db.schema_contract import require_scratch

CATALOG = TrainingCatalog.load(Path(__file__).parents[1] / 'fixtures/trainings/catalog.json')
TID = 'practice-basics'
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)


class _OneTransaction:
    def __init__(self, session: AsyncSession):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def commit(self) -> None:
        await self._session.flush()


@pytest_asyncio.fixture
async def db(monkeypatch):
    dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('MOLY_SCHEMA_TEST_DSN required')
    require_scratch(dsn)
    monkeypatch.setattr(training_catalog, 'current', lambda: CATALOG)

    async def graded(stage, answer, language, *, user_id):
        return Verdict(passed=True, judge='ai')

    monkeypatch.setattr(training_grading, 'grade_write', graded)
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'),
                                 poolclass=NullPool, connect_args={'statement_cache_size': 0})
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield _OneTransaction(session)
            await session.rollback()
    finally:
        await engine.dispose()


async def signup(db) -> str:
    uid = uuid.uuid4()
    await db.execute(text('INSERT INTO auth.users(id) VALUES (:id)'), {'id': uid})
    return str(uid)


def chosen(option_id: str, request_id: uuid.UUID | None = None) -> TrainingAnswerRequest:
    return TrainingAnswerRequest(client_request_id=request_id or uuid.uuid4(), option_id=option_id)


def written(answer: str) -> TrainingAnswerRequest:
    return TrainingAnswerRequest(client_request_id=uuid.uuid4(), text=answer)


async def submit(db, user, stage, body):
    return await trainings.submit(db, user, TID, stage, body, locale='ko', now=NOW)


async def rewards(db, user) -> list[tuple]:
    rows = await db.execute(text(
        "SELECT amount, balance_after FROM hay_transactions WHERE user_id = :u AND type = 'training_reward' "
        'ORDER BY id'
    ), {'u': uuid.UUID(user)})
    return [tuple(row) for row in rows]


async def test_full_run_restart_and_claim_write_valid_rows(db):
    user = await signup(db)
    balance = await db.scalar(text('SELECT hay_balance FROM profiles WHERE id = :u'), {'u': uuid.UUID(user)})
    assert (await trainings.start(db, user, TID, now=NOW))['next_stage'] == 1

    retry_id = uuid.uuid4()
    wrong = await submit(db, user, 1, chosen('a', retry_id))
    assert (wrong['result'], wrong['tries']['left'], wrong['tries']['next_refill_at']) == (
        'retry', 4, NOW + timedelta(hours=1))
    # The same request id replays the stored verdict from JSONB without spending another try.
    replay = await submit(db, user, 1, chosen('a', retry_id))
    assert (replay['result'], replay['hint'], replay['tries']['left']) == ('retry', wrong['hint'], 4)

    assert (await submit(db, user, 1, chosen('b')))['explanation']
    assert (await submit(db, user, 2, written('포장 실수')))['judge'] == 'ai'
    assert (await submit(db, user, 3, chosen('c')))['reward'] == {'granted': 15, 'balance_after': balance + 15}
    assert (await submit(db, user, 4, chosen('a')))['progress']['next_stage'] == 5
    await submit(db, user, 5, written('다음 장에 다시 쓰면 된다'))
    done = await submit(db, user, 6, written('오늘 끝까지 해낸 나에게 고마워'))
    assert done['progress'] == {'started': True, 'next_stage': None, 'passed_count': 6, 'completed': True}
    assert done['reward'] == {'granted': 40, 'balance_after': balance + 55}

    detail = await trainings.get_training(db, user, TID, locale='ja', now=NOW)
    assert [a['stage'] for a in detail['answers']] == [1, 2, 3, 4, 5, 6]
    assert detail['answers'][1]['text'] == '포장 실수' and detail['answers'][0]['option_id'] == 'b'
    assert detail['title'] == '練習の基本' and detail['tries']['left'] == 4

    again = await trainings.restart(db, user, TID, locale='ko', now=NOW)
    assert again['answers'] == [] and again['progress']['next_stage'] == 1 and again['tries']['left'] == 4
    await submit(db, user, 1, chosen('b'))
    await submit(db, user, 2, written('포장 실수'))
    assert (await submit(db, user, 3, chosen('c')))['reward'] is None
    assert await rewards(db, user) == [(15, balance + 15), (40, balance + 55)]

    await trainings.claim(db, user, TID, 2, TrainingClaimRequest(
        reason='should_pass', answer_text='포장 실수', verdict='retry', hint='조금 더 써 볼까요?'))
    assert await db.scalar(text('SELECT count(*) FROM training_claims WHERE user_id = :u'),
                           {'u': uuid.UUID(user)}) == 1
    cards = (await trainings.list_trainings(db, user, locale='ko'))['trainings']
    assert [(c['id'], c['passed_count'], c['completed']) for c in cards] == [
        ('mini', 0, False), (TID, 3, False)]


async def test_last_try_blocks_wrong_answers_until_the_hour_refills(db):
    user = await signup(db)
    for _ in range(5):
        await submit(db, user, 1, chosen('a'))
    with pytest.raises(Exception) as blocked:
        await submit(db, user, 1, chosen('b'))
    assert getattr(blocked.value, 'code', None) == 'TRAINING_NO_TRIES'
    refilled = await trainings.submit(db, user, TID, 1, chosen('b'), locale='ko', now=NOW + timedelta(hours=1))
    assert refilled['result'] == 'passed' and refilled['tries']['left'] == 1


async def test_reward_adds_to_the_balance_as_it_is_after_grading(db, monkeypatch):
    user = await signup(db)
    uid = uuid.UUID(user)
    for stage, body in [(1, chosen('b')), (2, written('포장 실수')), (3, chosen('c')), (4, chosen('a')),
                        (5, written('다음 장에 다시 쓰면 된다'))]:
        await submit(db, user, stage, body)
    db.expunge_all()  # 요청마다 세션이 새로 열리는 것과 같게 한다.
    before = await db.scalar(text('SELECT hay_balance FROM profiles WHERE id = :u'), {'u': uid})

    async def grade_while_an_ad_pays(stage, answer, language, *, user_id):
        await db.execute(text('UPDATE profiles SET hay_balance = hay_balance + 50 WHERE id = :u'), {'u': uid})
        return Verdict(passed=True, judge='ai')

    monkeypatch.setattr(training_grading, 'grade_write', grade_while_an_ad_pays)
    done = await trainings.submit(db, user, TID, 6, written('오늘 끝까지 해낸 나에게 고마워'), locale=None, now=NOW)
    assert done['reward'] == {'granted': 40, 'balance_after': before + 50 + 40}
