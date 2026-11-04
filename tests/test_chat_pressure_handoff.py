"""Same-request recovery owns one lease, classification, publication and charge."""
import asyncio
from datetime import datetime, timezone
import time
from unittest.mock import AsyncMock

import pytest

from app.core import errors
from app.core.time_utils import activity_date_for
from app.models.message import Message
from app.services import chat, crisis_chat, crisis_guard
from tests.test_guard_context_v2_integration_review import setup as setup, Session, request, UID


class ResumableSession(Session):
    def expunge_all(self):
        pass


@pytest.fixture
def handoff(setup, monkeypatch):
    gate, config = setup
    gate.profile.timezone = 'Asia/Seoul'
    gate.activity_date = activity_date_for(datetime.now(timezone.utc), gate.profile.timezone)
    decision = AsyncMock(return_value=crisis_guard.DecisionResult('ok', route='normal', case='not_applicable'))
    monkeypatch.setattr(crisis_chat, 'recorded_decision', decision)
    acquire = AsyncMock(wraps=chat.chat_turns.acquire)
    finish = AsyncMock(wraps=chat.chat_turns.finish_publish)
    monkeypatch.setattr(chat.chat_turns, 'acquire', acquire)
    monkeypatch.setattr(chat.chat_turns, 'finish_publish', finish)
    original = chat._context
    calls = []
    async def context(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise chat._HistoryReadPressure()
        return await original(*args, **kwargs)
    monkeypatch.setattr(chat, '_context', context)
    wait = AsyncMock(return_value=True)
    monkeypatch.setattr(chat, '_wait_for_context_progress', wait)
    return gate, config, decision, acquire, finish, calls, wait


async def execute(session=None):
    return await chat.post_message(session or ResumableSession(), UID, request(), 'handoff',
        deadline=time.monotonic()+8, pressure_deadline=time.monotonic()+12)


async def test_raw_pressure_reprepares_without_reacquiring_reclassifying_or_double_charging(handoff):
    _, _, decision, acquire, finish, contexts, wait = handoff
    session = ResumableSession()
    await execute(session)
    assert len(contexts) == 2
    wait.assert_awaited_once()
    decision.assert_awaited_once()
    acquire.assert_awaited_once()
    finish.assert_awaited_once()
    chat.llm.generate.assert_awaited_once()
    chat._accumulate_tokens.assert_awaited_once()
    assert len([x for x in session.added if isinstance(x, Message)]) == 2
    assert wait.await_args.args[2].lease == finish.await_args.kwargs['lease']


async def test_assembled_pressure_reloads_memory_and_preserves_one_embedding_cache(handoff, monkeypatch):
    _, _, decision, _, _, contexts, wait = handoff
    # Avoid the raw pressure and exercise the later assembled-input boundary.
    original = chat._context
    contexts.append({})
    monkeypatch.setattr(chat, '_context', original)
    count = 0
    def budget(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise chat.context_retention.RetentionError('context_input_budget')
    monkeypatch.setattr(chat.context_retention, 'assert_input_budget', budget)
    await execute()
    wait.assert_awaited_once()
    assert chat._recall_memory_v2.await_count == 2
    a, b = chat._recall_memory_v2.await_args_list
    assert a.kwargs['query_embedding_cache'] is b.kwargs['query_embedding_cache']
    decision.assert_awaited_once()
    chat.llm.generate.assert_awaited_once()


@pytest.mark.parametrize('outcome', ['no_progress', 'cancel', 'replaced_lease', 'second_pressure'])
async def test_failed_resume_never_publishes_or_charges(handoff, monkeypatch, outcome):
    *_, contexts, wait = handoff
    if outcome == 'no_progress':
        wait.return_value = False
    elif outcome == 'cancel':
        wait.side_effect = asyncio.CancelledError()
    elif outcome == 'replaced_lease':
        chat.chat_turns.verify_publish = AsyncMock(side_effect=[None, errors.AppError('CHAT_TURN_STALE', 409, 'stale')])
    else:
        monkeypatch.setattr(chat, '_context', AsyncMock(side_effect=chat._HistoryReadPressure))
    session = ResumableSession()
    with pytest.raises((errors.AppError, asyncio.CancelledError)):
        await execute(session)
    assert session.added == []
    chat._accumulate_tokens.assert_not_awaited()
    chat.llm.generate.assert_not_awaited()
    chat.chat_turns.release.assert_awaited_once()
    assert wait.await_count <= 1


@pytest.mark.parametrize('change', ['config', 'timezone', 'language', 'day', 'recent', 'barrier'])
async def test_changed_context_between_pause_and_resume_cancels(handoff, monkeypatch, change):
    gate, config, _, _, _, _, wait = handoff
    async def changed(*args):
        if change == 'config':
            config['crisis_guard_policy_version'] = 'changed'
        elif change == 'timezone':
            gate.profile.timezone = 'UTC'
        elif change == 'language':
            gate.profile.language = 'ja'
        elif change == 'day':
            gate.activity_date = gate.activity_date.replace(year=2025)
        elif change == 'recent':
            chat._guard_recent.return_value = [crisis_guard.GuardMessage('user', 'changed', message_id=9)]
        else:
            monkeypatch.setattr(chat.privacy, 'load_barrier', AsyncMock(return_value='changed'))
        return True
    wait.side_effect = changed
    session = ResumableSession()
    with pytest.raises(errors.AppError) as error:
        await execute(session)
    assert error.value.code == 'CHAT_TURN_STALE'
    assert session.added == []
    chat._accumulate_tokens.assert_not_awaited()


async def test_policy_change_during_generation_is_rejected_before_final_storage(handoff):
    _, config, _, _, finish, _, _ = handoff
    result = chat.llm.generate.return_value
    async def model(*args, **kwargs):
        config['crisis_guard_policy_version'] = 'changed-during-model'
        return result
    chat.llm.generate.side_effect = model
    session = ResumableSession()
    with pytest.raises(errors.AppError) as error:
        await execute(session)
    assert error.value.code == 'CHAT_TURN_STALE'
    assert session.added == []
    finish.assert_not_awaited()
    chat._accumulate_tokens.assert_not_awaited()


async def test_technical_decision_failure_keeps_original_fallback(handoff):
    handoff[2].return_value = crisis_guard.DecisionResult('timeout', route='normal')
    with pytest.raises(errors.AppError):
        await execute()
    handoff[-1].assert_not_awaited()


async def test_fixed_guide_never_enters_normal_pressure_preparation(handoff):
    handoff[2].return_value = crisis_guard.DecisionResult('ok', route='safety', case='self_intent_plan')
    await execute()
    assert handoff[-2] == []
    handoff[-1].assert_not_awaited()
    chat.llm.generate.assert_not_awaited()


async def test_pressure_failure_cleanup_cannot_wait_indefinitely(monkeypatch):
    async def inner(session, user_id, req, key, **kw):
        kw['_owner'].update(lease='own-token', pressure_active=True,
            pressure_response_deadline=time.monotonic()+0.03)
        raise errors.ai_unavailable()
    monkeypatch.setattr(chat, '_post_message_impl', inner)
    session = ResumableSession()
    session.rollback = AsyncMock(side_effect=lambda: asyncio.sleep(30))
    async def slow_rollback():
        await asyncio.sleep(30)
    session.rollback.side_effect = slow_rollback
    started = time.monotonic()
    with pytest.raises(errors.AppError):
        await chat.post_message(session, UID, request(), 'cleanup')
    assert time.monotonic()-started < 0.3


@pytest.mark.parametrize('failed', [False, True])
async def test_only_query_vector_is_reused_while_recall_executes_again(monkeypatch, failed):
    from types import SimpleNamespace
    from app.core import db
    from app.services import mem0_recall, memory_embeddings
    class ReadSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(db, 'get_sessionmaker', lambda: ReadSession)
    monkeypatch.setattr(chat, '_recall_adapter', lambda: object())
    embed = AsyncMock(side_effect=RuntimeError('synthetic') if failed else None,
                      return_value=[0.1, 0.2])
    monkeypatch.setattr(memory_embeddings, 'embed_query', embed)
    async def recall(session, user_id, **kw):
        await kw['embed_query'](kw['query'], timeout=1)
        return []
    recall_mock = AsyncMock(side_effect=recall)
    monkeypatch.setattr(mem0_recall, 'recall', recall_mock)
    cache = {}
    for _ in range(2):
        await chat._recall_memory_v2(chat._uid(UID), query='synthetic query',
            state=SimpleNamespace(serves_v2=True), language='en', query_embedding_cache=cache)
    embed.assert_awaited_once()
    assert recall_mock.await_count == 2


async def test_absolute_timer_already_covers_slow_pressure_reservation(handoff):
    async def slow(*args, **kwargs):
        await asyncio.sleep(30)
    chat.checkpoint_repo.request_pressure.side_effect = slow
    session = ResumableSession()
    started = time.monotonic()
    with pytest.raises(errors.AppError) as error:
        await chat.post_message(session, UID, request(), 'slow-reservation',
            deadline=started+8, pressure_deadline=started+0.35)
    assert error.value.code == 'AI_UNAVAILABLE'
    assert time.monotonic()-started < 0.6
    assert session.added == []
    chat._accumulate_tokens.assert_not_awaited()
    chat.chat_turns.release.assert_awaited_once()


async def test_disabled_writers_do_not_enter_inline_wait(handoff):
    handoff[1]['context_retention_v2_enabled'] = False
    with pytest.raises(errors.AppError):
        await execute()
    handoff[-1].assert_not_awaited()
