"""Resume reserves match the existing single-call fallback and actual recall timeout."""
import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.services import chat, llm
from app.services.agent import runtime
from tests.test_chat_pressure_handoff import handoff as handoff, setup as setup, execute
from tests.test_agent_runtime import _Clock, _FakeSteps, _text_step, _cfg, _FakeRegistry, _FakeTool, UID


async def test_existing_runtime_can_dispatch_one_plain_reply_with_two_seconds_left(monkeypatch):
    clock = _Clock()
    fake = _FakeSteps(_text_step('응.', purpose='chat'))
    tool = _FakeTool()
    monkeypatch.setattr(llm, 'generate_step', fake)
    result = await runtime.run_turn(['persona'], [{'role':'user','content':'synthetic'}],
        config=_cfg(), user_id=UID, language='ko', activity_date=date(2026,11,4),
        user_text='synthetic', registry=_FakeRegistry(tool), now=clock, deadline=clock()+2)
    assert len(fake.calls) == 1 and fake.calls[0]['tools'] is None
    assert fake.calls[0]['purpose'] == 'chat' and tool.ran == 0
    assert len(result.calls) == 1 and result.skipped == 'deadline'


@pytest.mark.parametrize('value', [None, (), (float('nan'),), (0.1,), (True,True)])
async def test_cached_budget_requires_valid_exact_vector(monkeypatch, value):
    monkeypatch.setattr(chat.settings, 'memory_embedding_dimensions', 2)
    cache = {chat._query_embedding_key('same'): value}
    assert not chat._valid_cached_query_embedding(cache, 'same')
    assert not chat._valid_cached_query_embedding(cache, 'other')
    assert chat._valid_cached_query_embedding({chat._query_embedding_key('same'):(0.1,0.2)}, 'same')


async def test_cached_resumed_search_is_actually_bounded_and_required_context_remains(handoff, monkeypatch):
    _, _, _, _, _, contexts, wait = handoff
    contexts.append({})  # reach the assembled-input pressure boundary
    monkeypatch.setattr(chat.settings, 'memory_embedding_dimensions', 2)
    chat.context_retention.load_required_facts.return_value = 'required_fact_marker'
    chat.contract_repo.published_text.return_value = 'contract_marker'
    calls = 0
    cancelled = asyncio.Event()
    original_wait_for = asyncio.wait_for
    timeouts = []
    async def observed_wait_for(future, timeout):
        timeouts.append(timeout)
        return await original_wait_for(future, timeout)
    monkeypatch.setattr(chat.asyncio, 'wait_for', observed_wait_for)
    async def recall(*args, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            kw['query_embedding_cache'][chat._query_embedding_key(kw['query'])] = (0.1,0.2)
            return 'optional_old_search_marker'
        try:
            await asyncio.sleep(2)
        finally:
            cancelled.set()
    monkeypatch.setattr(chat, '_recall_memory_v2', AsyncMock(side_effect=recall))
    budgets = 0
    def budget(*args, **kw):
        nonlocal budgets
        budgets += 1
        if budgets == 1:
            raise chat.context_retention.RetentionError('context_input_budget')
    monkeypatch.setattr(chat.context_retention, 'assert_input_budget', budget)
    await execute()
    state = wait.await_args.args[2]
    assert state.recall_timeout == 0.5 and state.model_reserve == 2.25
    assert timeouts == [1.5, 0.5]
    assert cancelled.is_set() and calls == 2
    chat.llm.generate.assert_awaited_once()
    system, messages = chat.llm.generate.await_args.args[:2]
    text = '\n'.join(system)+ '\n'+'\n'.join(m['content'] for m in messages)
    assert 'required_fact_marker' in text and 'contract_marker' in text
    assert 'optional_old_search_marker' not in text
    chat._accumulate_tokens.assert_awaited_once()
