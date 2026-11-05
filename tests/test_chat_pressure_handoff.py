"""Same-request recovery owns one lease, classification, publication and charge."""
import asyncio
from datetime import datetime, timedelta, timezone
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
    chat.checkpoint_repo.request_pressure.return_value = chat.checkpoint_repo.PressureReservation('pending', 400)
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
    assert wait.await_args.args[2].model_reserve == 4.25


@pytest.mark.parametrize('projection', [None, 'a1a721c8-184c-4c39-8f8a-5b5b51fce63b'])
async def test_online_projection_metadata_does_not_invalidate_unchanged_resume_policy(handoff, monkeypatch, projection):
    from app.services import retention_release_read

    _, config, decision, acquire, finish, _, wait = handoff

    async def selected_settings(session, keys):
        return {key: value for key, value in config.items() if key in keys}

    async def captured_controls(session, raw):
        # Real online controls add this nullable identity after the initial read.
        # The resume SELECT returns only its explicit policy keys, as PostgreSQL does.
        return {**raw, "context_retention_active_preparation": projection}, None

    monkeypatch.setattr(chat.config_store, 'get_config_values', selected_settings)
    monkeypatch.setattr(retention_release_read, 'capture_controls', captured_controls)
    session = ResumableSession()
    await execute(session)

    state = wait.await_args.args[2]
    assert 'context_retention_active_preparation' not in state.config_keys
    decision.assert_awaited_once()
    acquire.assert_awaited_once()
    finish.assert_awaited_once()
    chat.llm.generate.assert_awaited_once()
    assert len([item for item in session.added if isinstance(item, Message)]) == 2


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


@pytest.mark.parametrize('outcome', ['cancel', 'replaced_lease', 'unreadable_even_shortened'])
async def test_failed_resume_never_publishes_or_charges(handoff, monkeypatch, outcome):
    *_, contexts, wait = handoff
    if outcome == 'cancel':
        wait.side_effect = asyncio.CancelledError()
    elif outcome == 'replaced_lease':
        chat.chat_turns.verify_publish = AsyncMock(side_effect=[None, errors.AppError('CHAT_TURN_STALE', 409, 'stale')])
    else:
        # Even the one shortened-history preparation reports pressure: no second fallback.
        monkeypatch.setattr(chat, '_context', AsyncMock(side_effect=chat._HistoryReadPressure))
    session = ResumableSession()
    with pytest.raises((errors.AppError, asyncio.CancelledError)):
        await execute(session)
    assert session.added == []
    chat._accumulate_tokens.assert_not_awaited()
    chat.llm.generate.assert_not_awaited()
    chat.chat_turns.release.assert_awaited_once()
    assert wait.await_count <= 1


NOTICE = 'Earlier conversation may be missing'


def _sent_notice():
    return sum(NOTICE in message['content'] for message in chat.llm.generate.await_args.args[1])


@pytest.mark.parametrize('outcome', ['no_progress', 'second_pressure'])
async def test_failed_wait_answers_from_shortened_recent_history(handoff, monkeypatch, outcome):
    _, _, decision, acquire, finish, contexts, wait = handoff
    if outcome == 'no_progress':
        wait.return_value = False
    else:
        recorded = chat._context  # The fixture wrapper records every call it receives.
        async def pressure_until_shortened(*args, **kwargs):
            if not kwargs.get('truncate_unread'):
                contexts.append(kwargs)
                raise chat._HistoryReadPressure()
            return await recorded(*args, **kwargs)
        monkeypatch.setattr(chat, '_context', pressure_until_shortened)
    save_anchor = AsyncMock()
    monkeypatch.setattr(chat, '_save_anchor', save_anchor)
    session = ResumableSession()
    result = await execute(session)
    assert result.reply.content == '합성 답변이야.'
    assert contexts[-1]['truncate_unread'] is True
    assert not any(kw.get('truncate_unread') for kw in contexts[:-1])
    wait.assert_awaited_once()
    decision.assert_awaited_once()
    acquire.assert_awaited_once()
    finish.assert_awaited_once()
    chat.chat_turns.release.assert_not_awaited()
    chat.llm.generate.assert_awaited_once()
    chat._accumulate_tokens.assert_awaited_once()
    assert len([x for x in session.added if isinstance(x, Message)]) == 2
    assert _sent_notice() == 1
    # The anchor never moves for a shortened view; the summary goal is reserved again
    # through this reply so normal behavior returns once it publishes.
    save_anchor.assert_not_awaited()
    calls = chat.checkpoint_repo.request_pressure.await_args_list
    assert all('through_id' not in call.kwargs for call in calls[:-1])
    assert calls[-1].kwargs['through_id'] == int(result.reply.message_id)
    assert finish.await_args.kwargs['lease'] == wait.await_args.args[2].lease


async def test_long_stalled_publish_answers_without_waiting(handoff):
    *_, wait = handoff
    chat.checkpoint_repo.request_pressure.return_value = chat.checkpoint_repo.PressureReservation(
        'pending', 400, waiting_since=datetime.now(timezone.utc) - timedelta(seconds=61))
    session = ResumableSession()
    result = await execute(session)
    wait.assert_not_awaited()
    assert result.reply.content == '합성 답변이야.' and _sent_notice() == 1
    chat.chat_turns.release.assert_not_awaited()
    chat._accumulate_tokens.assert_awaited_once()


async def test_recent_pending_publish_still_waits(handoff):
    *_, wait = handoff
    chat.checkpoint_repo.request_pressure.return_value = chat.checkpoint_repo.PressureReservation(
        'pending', 400, waiting_since=datetime.now(timezone.utc) - timedelta(seconds=30))
    await execute()
    wait.assert_awaited_once()
    assert _sent_notice() == 0


async def test_shortened_history_that_still_does_not_fit_keeps_503(handoff, monkeypatch):
    *_, wait = handoff
    wait.return_value = False
    def over_budget(*args, **kwargs):
        raise chat.context_retention.RetentionError('context_input_budget')
    monkeypatch.setattr(chat.context_retention, 'assert_input_budget', over_budget)
    def fits(system, messages, **kwargs):
        # The essential-input check (no history notice) fits; every shortened prompt does not.
        return not any(NOTICE in message['content'] for message in messages)
    monkeypatch.setattr(chat.context_retention, 'fits_input_budget', fits)
    session = ResumableSession()
    with pytest.raises(errors.AppError) as caught:
        await execute(session)
    assert caught.value.code == 'AI_UNAVAILABLE'
    assert session.added == []
    chat.llm.generate.assert_not_awaited()
    chat._accumulate_tokens.assert_not_awaited()
    chat.chat_turns.release.assert_awaited_once()
    # Only the first pressure reserved the summary; the failed fallback does not repeat it.
    chat.checkpoint_repo.request_pressure.assert_awaited_once()


@pytest.mark.parametrize('change', ['config', 'config_deleted', 'timezone', 'language', 'day', 'recent', 'barrier'])
async def test_changed_context_between_pause_and_resume_cancels(handoff, monkeypatch, change):
    gate, config, _, _, _, _, wait = handoff
    async def changed(*args):
        if change == 'config':
            config['crisis_guard_policy_version'] = 'changed'
        elif change == 'config_deleted':
            config.pop('crisis_guard_policy_version')
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
    session = ResumableSession()
    with pytest.raises(errors.AppError) as caught:
        await execute(session)
    assert caught.value.code == 'AI_UNAVAILABLE'
    handoff[-1].assert_not_awaited()
    # No shortened-history answer either: the request is released without storage.
    assert len(handoff[-2]) == 1
    assert session.added == []
    chat.llm.generate.assert_not_awaited()
    chat._accumulate_tokens.assert_not_awaited()
    chat.chat_turns.release.assert_awaited_once()


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
    from types import SimpleNamespace
    handoff[1]['context_retention_v2_enabled'] = False
    session = ResumableSession()
    session.get_map['ChatContext'] = SimpleNamespace(anchor_message_id=0, memory_generation=0,
        last_active_at=None, retention_v5_adopted=True)
    # No inline wait; the same lease answers once from a shortened recent history.
    await execute(session)
    handoff[-1].assert_not_awaited()
    assert handoff[-2][-1]['truncate_unread'] is True and _sent_notice() == 1
    chat.chat_turns.release.assert_not_awaited()


@pytest.mark.parametrize('status', ['disabled', 'not_adopted', 'no_source', 'stale_generation'])
async def test_reservation_without_a_valid_target_never_waits(handoff, status):
    chat.checkpoint_repo.request_pressure.return_value = chat.checkpoint_repo.PressureReservation(status)
    session = ResumableSession()
    await execute(session)
    handoff[-1].assert_not_awaited()
    assert handoff[-2][-1]['truncate_unread'] is True and _sent_notice() == 1
    chat._accumulate_tokens.assert_awaited_once()
    assert len([x for x in session.added if isinstance(x, Message)]) == 2


def _stored(uid, message_id, chars=133):
    # 133 Hangul syllables = 399 UTF-8 bytes; 120 of them exceed the foreground read limit.
    return Message(id=message_id, user_id=uid, sender='user' if message_id % 2 else 'moly',
                   kind='normal', content='가' * chars, activity_date=datetime(2026, 11, 5).date(),
                   created_at=None)


class _PagedSession:
    def __init__(self, page):
        self.pages = [page]

    async def execute(self, stmt, params=None):
        from tests.test_chat import _Result
        return _Result(self.pages.pop(0) if self.pages else [])


@pytest.mark.parametrize('truncate', [False, True])
async def test_shortened_read_keeps_newest_rows_without_anchor_or_coverage(truncate):
    import uuid
    from app.services.context_provenance import VerifiedCoverage
    uid = chat._uid(UID)
    page = [_stored(uid, message_id) for message_id in range(130, 10, -1)]  # newest first
    coverage = VerifiedCoverage(uid, 0, uuid.UUID(int=9), 10, 'a' * 64)
    kwargs = dict(current_text='synthetic current', current_date=datetime(2026, 11, 5).date(),
                  retention_enabled=True, memory_generation=0, coverage=coverage,
                  truncate_unread=truncate)
    if not truncate:
        with pytest.raises(chat._HistoryReadPressure):
            await chat._context(_PagedSession(page), uid, 0, **kwargs)
        return
    window = await chat._context(_PagedSession(page), uid, 0, **kwargs)
    limit = chat.context_retention.FOREGROUND_INPUT_LIMIT
    history = [entry for entry in window.conversation if entry.source_ref is not None]
    kept_ids = [entry.source_ref.message_id for entry in history]
    lead_ids = [message.id for message in window.lead]
    assert window.incomplete is True
    # The anchor comes only from the published boundary (through=10), never from the cut.
    assert window.new_anchor == 11
    # Dropped rows are not granted as summarized; nothing fetched is covered either.
    assert window.coverage.source_refs == frozenset()
    # The kept view is the newest contiguous suffix within the read limit.
    assert sorted(lead_ids + kept_ids) == list(range(min(lead_ids + kept_ids), 131))
    assert min(lead_ids + kept_ids) > 11
    assert chat.context_retention.source_tokens(
        [m for m in page if m.id in set(lead_ids + kept_ids)]) <= limit
    # The truncated size is not reported as the whole history.
    assert window.retention_source_bytes > limit
    assert window.conversation[-1]['content'].endswith('synthetic current')
    assert window.conversation[-1].source_ref is None
    assert history[0]['role'] == 'user'


def test_trim_drops_only_oldest_sourced_history_and_a_leading_reply():
    import hashlib
    from app.services.context_provenance import HistoryEntry, SourceRef
    uid = chat._uid(UID)
    def sourced(message_id, role, text):
        ref = SourceRef(uid, message_id, 0, hashlib.sha256(text.encode()).hexdigest())
        return HistoryEntry(role, text, source_ref=ref)
    lead = sourced(1, 'system', 'old lead')
    convo = [lead, sourced(2, 'user', 'u2'), sourced(3, 'assistant', 'a3'),
             sourced(4, 'user', 'u4'), sourced(5, 'assistant', 'a5'),
             {'role': 'system', 'content': 'volatile summary memory facts'},
             {'role': 'user', 'content': 'current'}]
    budget = {'size': 5}
    def fits(items):
        return len(items) <= budget['size']
    trimmed = chat._trim_oldest_history(convo, fits)
    # lead, u2 dropped; a3 then leads and is dropped with them.
    assert [item['content'] for item in trimmed] == ['u4', 'a5', 'volatile summary memory facts', 'current']
    budget['size'] = 2
    assert [item['content'] for item in chat._trim_oldest_history(convo, fits)] == [
        'volatile summary memory facts', 'current']
    budget['size'] = 1
    assert chat._trim_oldest_history(convo, fits) is None
    budget['size'] = 7
    assert chat._trim_oldest_history(convo, fits) == convo


async def test_wait_stops_early_enough_to_reprepare(monkeypatch):
    from types import SimpleNamespace
    session = SimpleNamespace(expunge_all=lambda: None, commit=AsyncMock(), rollback=AsyncMock())
    started = time.monotonic()
    state = SimpleNamespace(deadline=started + chat._PRESSURE_REPREPARE_S + 0.05, model_reserve=0,
                            generation=0, baseline_through=100, required_through=400)
    monkeypatch.setattr(chat, '_validate_pressure_resume', AsyncMock())
    monkeypatch.setattr(chat.checkpoint_repo, 'load_latest', AsyncMock(return_value=None))
    monkeypatch.setattr(chat.checkpoint_repo, 'coverage_for_checkpoint', AsyncMock(return_value=None))
    assert not await chat._wait_for_context_progress(session, 'synthetic-user', state)
    # It stops before deadline - model_reserve - reprepare, not at deadline - model_reserve.
    assert time.monotonic() - started < chat._PRESSURE_REPREPARE_S
