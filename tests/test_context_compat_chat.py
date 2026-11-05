"""Actual foreground assembly keeps legacy background and fenced new context."""
from dataclasses import replace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.core.errors import AppError
from app.services import chat, crisis_chat, crisis_guard
from app.services.context_compat import Baseline
from tests.test_guard_context_v2_integration_review import setup as setup, Session, request, UID
from tests.test_chat_retention import Pages, messages
from tests.test_guard_legacy_context_compat import CASES


def baseline():
    return Baseline(str(uuid.UUID(int=501)), str(UID), 0, 0, 100, 70,
        None, 0, None, None, None, 80, None, None)


async def normal(monkeypatch):
    monkeypatch.setattr(crisis_chat, 'recorded_decision', AsyncMock(return_value=
        crisis_guard.DecisionResult('ok', route='normal', case='not_applicable')))


async def test_reader_starts_after_legacy_boundary_without_claiming_v5_coverage():
    rows = messages(3, start=101)
    session = Pages(rows)
    result = await chat._context(session, UID, 500, retention_enabled=True,
        legacy_source_after=100, compact_crises=False)
    assert len(result.conversation) == 3 and result.coverage is None
    assert 101 in session.queries[0].values()
    assert result.new_anchor is None


@pytest.mark.parametrize('leading', [False, True])
@pytest.mark.parametrize('language,crisis,safety,shift,neutral,current,old_summary', CASES)
async def test_legacy_manual_crisis_never_reappears_as_raw_or_system_lead(
        leading,language,crisis,safety,shift,neutral,current,old_summary):
    from tests.test_guard_input_isolation import Rows, row
    original = [row(101,'user','normal',crisis),row(102,'moly','normal',safety),
        row(103,'user','normal',shift),row(104,'moly','normal',neutral)]
    if leading:
        original = original[1:]
    before = [(m.id,m.kind,m.content) for m in original]
    result = await chat._context(Rows(original),UID,101,retention_enabled=True,
        legacy_source_after=100,legacy_projection=True,language=language,
        current_text=current,compact_crises=False)
    wire='\n'.join([*(m['content'] for m in result.conversation),*(m.content for m in result.lead)])
    assert safety not in wire and crisis not in wire
    assert shift in wire and result.coverage is None
    assert [(m.id,m.kind,m.content) for m in original]==before
    assert result.retention_source_bytes==chat.context_retention.source_tokens(original)


async def test_old_background_and_current_verified_blocks_both_reach_actual_model(setup, monkeypatch):
    await normal(monkeypatch)
    chat.context_compat.load.return_value = baseline()
    chat.context_compat.legacy_summary.return_value = 'SYNTHETIC_OLD_UNRELATED_BACKGROUND'
    chat.context_retention.load_required_facts.return_value = 'SYNTHETIC_CURRENT_VERIFIED_FACT'
    chat.contract_repo.published_text.return_value = 'SYNTHETIC_EXISTING_REQUEST'
    await chat.post_message(Session(), UID, request('오늘 이야기'), 'compat-background')
    system, conversation = chat.llm.generate.await_args.args[:2]
    wire = '\n'.join([*system, *(entry['content'] for entry in conversation)])
    for expected in ('SYNTHETIC_OLD_UNRELATED_BACKGROUND', 'SYNTHETIC_CURRENT_VERIFIED_FACT',
                     'SYNTHETIC_EXISTING_REQUEST', 'precedence'):
        assert expected in wire
    assert chat._record_memory_v2.await_count == 1
    assert chat.contract_repo.published_text.await_args.kwargs['compat_baseline'] == baseline()


@pytest.mark.parametrize('text', ['아까 말한 건 잘못됐어', '그 내용은 잊어줘'])
async def test_pending_correction_does_not_reinject_opaque_old_summary(setup, monkeypatch, text):
    await normal(monkeypatch)
    chat.context_compat.legacy_summary.return_value = 'SYNTHETIC_STALE_OLD_FACT'
    chat.context_retention.pending_correction.return_value = True
    await chat.post_message(Session(), UID, request(text), 'compat-correction')
    system, conversation = chat.llm.generate.await_args.args[:2]
    assert all('SYNTHETIC_STALE_OLD_FACT' not in item for item in system)
    assert all('SYNTHETIC_STALE_OLD_FACT' not in entry['content'] for entry in conversation)
    chat.context_compat.invalidate_legacy_summary.assert_awaited_once()


async def test_baseline_change_during_generation_never_stores_or_charges(setup, monkeypatch):
    await normal(monkeypatch)
    chat.context_compat.matches_current.return_value = False
    session = Session()
    with pytest.raises(AppError) as error:
        await chat.post_message(session, UID, request(), 'compat-baseline-race')
    assert error.value.code == 'CHAT_TURN_STALE'
    assert session.added == []
    chat._accumulate_tokens.assert_not_awaited()
    chat._record_memory_v2.assert_not_awaited()


async def test_legacy_invalidation_during_generation_cancels_stale_reply(setup, monkeypatch):
    await normal(monkeypatch)
    chat.context_compat.legacy_summary.side_effect = ['old synthetic summary', '']
    session = Session()
    with pytest.raises(AppError) as error:
        await chat.post_message(session, UID, request(), 'compat-legacy-race')
    assert error.value.code == 'CHAT_TURN_STALE' and session.added == []
    chat._accumulate_tokens.assert_not_awaited()


async def test_completed_correction_cannot_restore_invalidated_legacy_summary(setup, monkeypatch):
    await normal(monkeypatch)
    chat.context_compat.load.return_value = replace(baseline(), legacy_checkpoint_id=str(uuid.UUID(int=601)),
        legacy_checkpoint_through=100, legacy_checkpoint_hash='a'*64,
        legacy_checkpoint_version='conversation-checkpoint-summary-v4', legacy_summary_hash='b'*64)
    chat.context_compat.legacy_summary.return_value = ''
    assert not chat.context_retention.pending_correction.return_value
    await chat.post_message(Session(), UID, request('전에 얘기한 거 기억나?'), 'compat-quarantined')
    system, conversation = chat.llm.generate.await_args.args[:2]
    wire = '\n'.join([*system, *(entry['content'] for entry in conversation)])
    assert 'The old opaque summary is unavailable or invalidated' in wire
    assert '[Historical background from the previous summary format]' not in wire


async def test_disabling_new_processing_keeps_compatible_reader_for_existing_baseline(setup, monkeypatch):
    from app.models.chat_context import ChatContext
    await normal(monkeypatch)
    _, config = setup
    config.update(context_retention_v2_enabled=False, context_retention_active_preparation=None)
    chat.context_retention.enabled_now.return_value = False
    session = Session()
    session.get_map['ChatContext'] = ChatContext(user_id=UID,memory_generation=0,
        anchor_message_id=101,retention_compat_json=baseline().payload())
    context=AsyncMock(return_value=chat.ContextWindow(conversation=[],new_anchor=None,
        lead=[],checkpoint_keep_from=None,incomplete=False))
    monkeypatch.setattr(chat,'_context',context)
    # Production capture_controls reads these actual rows, not just its input dict.
    previous_execute=session.execute
    async def execute(statement,params=None):
        from types import SimpleNamespace
        if 'SELECT key,value FROM app_config' in str(statement):
            return SimpleNamespace(all=lambda:list(config.items()))
        return await previous_execute(statement,params)
    session.execute=execute
    await chat.post_message(session,UID,request(),'compat-writers-disabled')
    assert context.await_args.kwargs['retention_enabled'] is True
    chat.context_compat.load.assert_awaited_once()


async def test_fixed_guard_never_needs_compatibility_or_old_documents(setup, monkeypatch):
    monkeypatch.setattr(crisis_chat, 'recorded_decision', AsyncMock(return_value=
        crisis_guard.DecisionResult('ok', route='safety', case='self_intent_plan')))
    chat.context_compat.load.side_effect = RuntimeError('legacy storage unavailable')
    result = await chat.post_message(Session(), UID, request(), 'compat-guard-bypass')
    assert result.reply.content == crisis_guard.fixed_reply('ko')
    chat.context_compat.load.assert_not_awaited()
    chat.context_compat.matches_current.assert_not_awaited()
    chat.llm.generate.assert_not_awaited()


async def test_existing_unbound_v5_chain_is_never_reinitialized(setup, monkeypatch):
    await normal(monkeypatch)
    chat.context_compat.load.return_value = None
    initialize = AsyncMock(side_effect=RuntimeError('must not capture another root'))
    monkeypatch.setattr(chat.context_compat, 'initialize', initialize)
    class ExistingV5(Session):
        async def scalar(self, statement, params=None):
            if str(statement) == str(chat.context_retention._ADOPTED_USER):
                return True
            return await super().scalar(statement, params)
    await chat.post_message(ExistingV5(), UID, request(), 'compat-existing-v5')
    initialize.assert_not_awaited()
    chat.context_compat.legacy_summary.assert_not_awaited()


@pytest.mark.parametrize('generation', [0, 1])
async def test_initial_capture_inherits_old_visible_boundary_not_current_input(setup, monkeypatch, generation):
    from app.models.chat_context import ChatContext
    from app.services import checkpoint
    await normal(monkeypatch)
    chat.context_compat.load.return_value = None
    captured = replace(baseline(), generation=generation)
    initialize = AsyncMock(return_value=captured)
    monkeypatch.setattr(chat.context_compat, 'initialize', initialize)
    monkeypatch.setattr(chat.context_retention, 'capture_privacy_epoch', AsyncMock(return_value=0))
    monkeypatch.setattr(chat.settings, 'context_checkpoint_enabled', True)
    cp = checkpoint.Checkpoint(uuid.UUID(int=502), 100, 'synthetic old summary',
        checkpoint.LEGACY_SUMMARIZER_VERSION, 'a'*64)
    async def load(session, uid, **kwargs):
        return None if kwargs.get('retention_enabled') else cp
    chat.checkpoint_repo.load_latest.side_effect = load
    context = AsyncMock(return_value=chat.ContextWindow(conversation=[], new_anchor=None,
        lead=[], checkpoint_keep_from=None, incomplete=False))
    monkeypatch.setattr(chat, '_context', context)
    session = Session()
    session.get_map['ChatContext'] = ChatContext(user_id=UID, memory_generation=generation,
        anchor_message_id=70)
    await chat.post_message(session, UID, request('synthetic current input'), 'compat-capture')
    initialize.assert_awaited_once()
    assert initialize.await_args.kwargs['source_after'] == 100
    first, actual = context.await_args_list
    assert 'current_text' not in first.kwargs and first.kwargs['retention_enabled'] is False
    assert actual.kwargs['legacy_source_after'] == 100


async def test_correction_backlog_answers_conservatively_instead_of_503(setup, monkeypatch):
    """처리 안 된 메시지가 많아 정정 확인을 못 해도 503이 아니라 보수적으로 답한다."""
    await normal(monkeypatch)
    chat.context_compat.load.return_value = baseline()
    chat.context_compat.legacy_summary.return_value = 'SYNTHETIC_STALE_OLD_FACT'
    chat.contract_repo.published_text.return_value = 'SYNTHETIC_EXISTING_REQUEST'
    chat.context_retention.pending_correction.side_effect = chat.context_retention.RetentionError(
        'correction_processing_backlog')
    await chat.post_message(Session(), UID, request('오늘 이야기'), 'compat-backlog')
    system, conversation = chat.llm.generate.await_args.args[:2]
    wire = '\n'.join([*system, *(entry['content'] for entry in conversation)])
    assert 'SYNTHETIC_STALE_OLD_FACT' not in wire and 'SYNTHETIC_EXISTING_REQUEST' not in wire
    # 정정이 확인된 것은 아니므로 옛 요약을 영구히 무효화하지 않는다.
    chat.context_compat.invalidate_legacy_summary.assert_not_awaited()


async def test_other_correction_scan_errors_still_fail_closed(setup, monkeypatch):
    await normal(monkeypatch)
    chat.context_retention.pending_correction.side_effect = chat.context_retention.RetentionError(
        'correction_source_order')
    with pytest.raises(AppError) as error:
        await chat.post_message(Session(), UID, request('오늘 이야기'), 'compat-scan-error')
    assert error.value.code == 'AI_UNAVAILABLE'
