"""Training service flow on a disposable local DB: ORM mapping, CHECK constraints and the hay ledger.

Service commits become flushes so the whole flow shares one transaction that is rolled back.
"""
from __future__ import annotations

import os
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.schemas.trainings import TrainingAnswerRequest, TrainingClaimRequest
from app.services import llm, training_catalog, training_grading, trainings
from app.services.training_catalog import TrainingCatalog
from app.services.training_grading import Verdict, grade_write as real_grade_write
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


async def test_full_run_and_claim_write_valid_rows(db):
    user = await signup(db)
    balance = await db.scalar(text('SELECT hay_balance FROM profiles WHERE id = :u'), {'u': uuid.UUID(user)})
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
    assert done['progress'] == {'next_stage': None, 'passed_count': 6, 'completed': True}
    assert done['reward'] == {'granted': 40, 'balance_after': balance + 55}

    detail = await trainings.get_training(db, user, TID, locale='ja', now=NOW)
    assert [a['stage'] for a in detail['answers']] == [1, 2, 3, 4, 5, 6]
    assert detail['answers'][1]['text'] == '포장 실수' and detail['answers'][0]['option_id'] == 'b'
    assert detail['title'] == '練習の基本' and detail['tries']['left'] == 4

    assert await rewards(db, user) == [(15, balance + 15), (40, balance + 55)]

    await trainings.claim(db, user, TID, 2, TrainingClaimRequest(
        reason='should_pass', answer_text='포장 실수', verdict='retry', hint='조금 더 써 볼까요?'))
    assert await db.scalar(text('SELECT count(*) FROM training_claims WHERE user_id = :u'),
                           {'u': uuid.UUID(user)}) == 1
    cards = (await trainings.list_trainings(db, user, locale='ko'))['trainings']
    assert [(c['id'], c['passed_count'], c['completed']) for c in cards] == [
        ('mini', 0, False), (TID, 6, True)]


async def test_last_try_blocks_wrong_answers_until_the_hour_refills(db):
    user = await signup(db)
    for _ in range(5):
        await submit(db, user, 1, chosen('a'))
    with pytest.raises(Exception) as blocked:
        await submit(db, user, 1, chosen('b'))
    assert getattr(blocked.value, 'code', None) == 'TRAINING_NO_TRIES'
    refilled = await trainings.submit(db, user, TID, 1, chosen('b'), locale='ko', now=NOW + timedelta(hours=1))
    assert refilled['result'] == 'passed' and refilled['tries']['left'] == 1


@pytest.mark.parametrize('answer', ['뭐래', 'ㅋㅋㅋ', '!!!'])
async def test_reaction_deducts_once_without_ai_answer_or_reward(db, monkeypatch, answer):
    user = await signup(db)
    await submit(db, user, 1, chosen('b'))
    monkeypatch.setattr(training_grading, 'grade_write', real_grade_write)

    async def unexpected_ai(*args, **kwargs):
        pytest.fail('A reaction must be rejected before AI or its fallback')

    monkeypatch.setattr(llm, 'generate', unexpected_ai)
    request = written(answer)
    result = await submit(db, user, 2, request)
    replay = await submit(db, user, 2, request)
    assert replay == result
    assert (result['result'], result['judge'], result['tries']['left']) == ('retry', 'rule', 4)
    assert result['progress']['next_stage'] == 2
    args = {'u': uuid.UUID(user)}
    assert await db.scalar(text('SELECT count(*) FROM training_submissions WHERE user_id=:u AND stage=2'), args) == 1
    assert await db.scalar(text('SELECT count(*) FROM training_answers WHERE user_id=:u AND stage=2'), args) == 0
    assert await rewards(db, user) == []


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


async def test_intervening_request_does_not_erase_replay_receipt(db):
    user = await signup(db)
    request = chosen('a')
    await submit(db, user, 1, request)
    await submit(db, user, 1, chosen('c'))
    assert (await submit(db, user, 1, request))['tries']['left'] == 3
    count = await db.scalar(text('SELECT count(*) FROM training_submissions WHERE user_id=:u'), {'u': uuid.UUID(user)})
    assert count == 2


async def test_real_sessions_share_one_grading_reservation(monkeypatch):
    import asyncio
    from app.core.errors import AppError

    dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('MOLY_SCHEMA_TEST_DSN required')
    require_scratch(dsn)
    monkeypatch.setattr(training_catalog, 'current', lambda: CATALOG)
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'), poolclass=NullPool)
    uid = uuid.uuid4()
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = 0
    request = written('내가 못해서')
    async def grade(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        return Verdict(False, 'ai', '다른 이유를 찾아볼까요?')
    monkeypatch.setattr(training_grading, 'grade_write', grade)
    async def send():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await submit(session, str(uid), 2, request)
    first = None
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await session.execute(text('INSERT INTO auth.users(id) VALUES (:u)'), {'u': uid})
            await submit(session, str(uid), 1, chosen('b'))
        first = asyncio.create_task(send())
        await asyncio.wait_for(entered.wait(), 5)
        rest = await asyncio.wait_for(asyncio.gather(*(send() for _ in range(7)), return_exceptions=True), 5)
        assert calls == 1
        assert all(isinstance(r, AppError) and r.code == 'TRAINING_SUBMISSION_PENDING' for r in rest)
        finish.set()
        original = await first
        assert original['tries']['left'] == 4
        assert await send() == original
        assert calls == 1
    finally:
        finish.set()
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)
        async with AsyncSession(engine) as session:
            await session.execute(text('DELETE FROM auth.users WHERE id=:u'), {'u': uid})
            await session.commit()
        await engine.dispose()


@pytest.mark.parametrize('language', ['ko', 'en', 'ja'])
async def test_active_courses_complete_all_90_stages_with_their_keys_and_rubrics(db, monkeypatch, language):
    catalog = TrainingCatalog.load()
    monkeypatch.setattr(training_catalog, 'current', lambda: catalog)
    user = await signup(db)
    cases = json.loads((Path(__file__).parents[2] / 'content/trainings/grading-eval.json').read_text())
    replies = {(case['course'], case['stage']): case['answers'] for case in cases if case['kind'] == 'natural_short'}
    for course in catalog.trainings:
        if course.archived:
            continue
        for stage in course.stages:
            body = chosen(next(o.id for o in stage.options if o.correct)) if stage.answer_type == 'pick' else written(stage.grading.example.pick(language) if stage.grading.example else replies[(course.id, stage.number)][language])
            result = await trainings.submit(db, user, course.id, stage.number, body, locale=language, now=NOW)
            assert result['result'] == 'passed', (course.id, stage.number)
            assert result['tries']['left'] == 5
        assert result['progress']['completed'] and result['progress']['passed_count'] == 30
        detail = await trainings.get_training(db, user, course.id, locale=language, now=NOW)
        assert len(detail['answers']) == 30
    cards = (await trainings.list_trainings(db, user, locale=language))['trainings']
    assert len(cards) == 3 and all(c['completed'] for c in cards)
    assert sum(amount for amount, _ in await rewards(db, user)) == 1060


@pytest.mark.parametrize('failure', ['timeout', 'provider', 'malformed'])
async def test_fallback_concurrent_reward_commits_once(monkeypatch, failure):
    """Real independent transactions: AI failure, duplicate HTTP-equivalent calls, replay."""
    import asyncio
    from app.core.errors import AppError
    from app.services import llm

    dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('MOLY_SCHEMA_TEST_DSN required')
    require_scratch(dsn)
    monkeypatch.setattr(training_catalog, 'current', lambda: CATALOG)
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'), poolclass=NullPool)
    uid = uuid.uuid4()
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = 0
    request = written('오늘 끝까지 해낸 내가 고맙다')
    first = None

    async def normal(*args, **kwargs):
        return llm.LLMResult(text='{"pass": true}', input_tokens=1, output_tokens=1)

    async def broken(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        if failure == 'timeout':
            await asyncio.sleep(60)  # The real grading deadline must cancel this.
        if failure == 'provider':
            raise RuntimeError('provider unavailable')
        return llm.LLMResult(text='not json', input_tokens=1, output_tokens=1)

    async def send(body=request):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await submit(session, str(uid), 6, body)

    try:
        monkeypatch.setattr(llm, 'generate', normal)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await session.execute(text('INSERT INTO auth.users(id) VALUES (:u)'), {'u': uid})
            await submit(session, str(uid), 1, chosen('a'))
            for stage, body in [(1, chosen('b')), (2, written('포장 실수')), (3, chosen('c')),
                                (4, chosen('a')), (5, written('다음 장에 다시 쓰면 된다'))]:
                await submit(session, str(uid), stage, body)
            before = await session.scalar(text('SELECT hay_balance FROM profiles WHERE id=:u'), {'u': uid})
        monkeypatch.setattr(llm, 'generate', broken)
        monkeypatch.setattr(training_grading, 'AI_DEADLINE_S', 2.0)
        first = asyncio.create_task(send())
        await asyncio.wait_for(entered.wait(), 5)
        blocked = await asyncio.gather(*(send() for _ in range(7)), return_exceptions=True)
        assert all(isinstance(r, AppError) and r.code == 'TRAINING_SUBMISSION_PENDING' for r in blocked)
        finish.set()
        result = await asyncio.wait_for(first, 5)
        assert (result['result'], result['judge'], result['tries']['left']) == ('passed', 'fallback', 4)
        assert result['progress']['completed']
        assert result['reward'] == {'granted': 40, 'balance_after': before + 40}
        replays = await asyncio.gather(*(send() for _ in range(8)))
        assert all(r == result for r in replays)
        with pytest.raises(AppError) as duplicate:
            await send(written(request.text))
        assert duplicate.value.code == 'TRAINING_STAGE_NOT_CURRENT'
        assert calls == 1
        async with AsyncSession(engine) as session:
            args = {'u': uid}
            assert await session.scalar(text('SELECT count(*) FROM training_answers WHERE user_id=:u'), args) == 6
            assert await session.scalar(text('SELECT count(*) FROM training_rewards WHERE user_id=:u AND stage=6'), args) == 1
            assert await session.scalar(text("SELECT count(*) FROM hay_transactions WHERE user_id=:u AND type='training_reward' AND amount=40"), args) == 1
            assert await session.scalar(text('SELECT hay_balance FROM profiles WHERE id=:u'), args) == before + 40
            assert await session.scalar(text('SELECT tries_left FROM training_tries WHERE user_id=:u'), args) == 4
            assert await session.scalar(text('SELECT refill_from FROM training_tries WHERE user_id=:u'), args) == NOW
            assert await session.scalar(text('SELECT count(*) FROM training_submissions WHERE user_id=:u AND stage=6'), args) == 1
            assert await session.scalar(text('SELECT answer_text FROM training_answers WHERE user_id=:u AND stage=6'), args) == request.text
    finally:
        finish.set()
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)
        async with AsyncSession(engine) as session:
            await session.execute(text('DELETE FROM auth.users WHERE id=:u'), {'u': uid})
            await session.commit()
        await engine.dispose()


@pytest.mark.parametrize('reason', ['wrong_hint', 'should_pass', 'should_fail', 'technical_error', 'other'])
async def test_claim_categories_persist_without_changing_progress(db, reason):
    user = await signup(db)
    await trainings.claim(db, user, TID, 2, TrainingClaimRequest(
        reason=reason, answer_text='포장 실수', verdict='retry'))
    args = {'u': uuid.UUID(user)}
    assert await db.scalar(text('SELECT reason FROM training_claims WHERE user_id=:u'), args) == reason
    assert await db.scalar(text('SELECT count(*) FROM training_progress WHERE user_id=:u'), args) == 0
    assert await rewards(db, user) == []


async def test_refusal_full_course_explanations_rewards_and_replay_are_isolated(db, monkeypatch):
    catalog = TrainingCatalog.load()
    monkeypatch.setattr(training_catalog, 'current', lambda: catalog)
    course = catalog.get('refusal-practice-v1')
    user = await signup(db)
    initial = await db.scalar(text('SELECT hay_balance FROM profiles WHERE id = :u'), {'u': uuid.UUID(user)})
    granted = 0
    for stage in course.stages:
        body = (chosen(next(o.id for o in stage.options if o.correct)) if stage.answer_type == 'pick'
                else written('이 단계의 합성 테스트 답변'))
        result = await trainings.submit(db, user, course.id, stage.number, body, locale='ko', now=NOW)
        assert result['result'] == 'passed'
        assert result['explanation'] == (stage.explanation.ko if stage.explanation else None)
        assert 'example' not in result
        if stage.reward_hay:
            granted += stage.reward_hay
            assert result['reward'] == {'granted': stage.reward_hay, 'balance_after': initial + granted}
        again = await trainings.submit(db, user, course.id, stage.number, body, locale='ko', now=NOW)
        assert again == result
    assert result['progress'] == {'next_stage': None, 'passed_count': 30, 'completed': True}
    assert granted == 580
    assert len(await rewards(db, user)) == 5
    detail = await trainings.get_training(db, user, course.id, locale='ko', now=NOW)
    assert len(detail['answers']) == 30
    cards = (await trainings.list_trainings(db, user, locale='ko'))['trainings']
    assert [(c['id'], c['passed_count']) for c in cards] == [
        ('positive-circuit-v2', 0), ('self-acceptance-v2', 0), ('refusal-practice-v1', 30)]
