"""Displayed-opening wire/provenance tests; no network, database or real models."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import crisis_guard, greeting_context as g, llm, context_retention
from app.services.agent import runtime
from app.services.context_provenance import HistoryEntry, SourceRef, VerifiedCoverage, without_covered
from tests.test_agent_runtime import (_FakeRegistry, _FakeSteps, _FakeTool, _FakeToolSession,
                                     _call, _calls_step, _cfg, _text_step)

UID=uuid.UUID(int=1)
GID=uuid.UUID(int=2)
DAY=date(2026,11,5)
ISSUED=datetime(2026,11,5,1,2,3,tzinfo=timezone.utc)


def opening(text='Synthetic displayed opening'):
    return g.OpeningSnapshot(UID,GID,text,ISSUED,DAY,None)


def row(**changes):
    return SimpleNamespace(**dict({'user_id':UID,'id':GID,'created_at':ISSUED,'activity_date':DAY,
                                   'committed_message_id':None},**changes))


def source(identity,role,text):
    return HistoryEntry(role,text,source_ref=SourceRef(UID,identity,0,hashlib.sha256(text.encode()).hexdigest()))


def history(current='me too'):
    return [source(1,'user','Covered old breakfast'),source(2,'assistant','Covered old menu'),
            HistoryEntry('system','Current/background blocks'),HistoryEntry('user',current)]


def assert_adjacent(messages,text,current):
    index=next(i for i,m in enumerate(messages) if m['role']=='user' and m.get('content')==current)
    assert messages[index-1]=={'role':'assistant','content':text}
    assert sum(m.get('content')==text for m in messages)==1


def test_snapshot_is_immutable_and_does_not_expose_body_in_repr():
    snapshot=g.capture(row(),user_id=UID,decoded_text='Synthetic private opening')
    assert snapshot==opening('Synthetic private opening') and len(snapshot.fingerprint)==64
    assert 'Synthetic private opening' not in repr(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.text='modified'


@pytest.mark.parametrize('changes',[{'owner_id':uuid.UUID(int=3)},{'greeting_id':uuid.UUID(int=3)},
    {'text':'different decoded text'},{'issued_at':None},{'activity_date':date(2026,11,4)},{'committed_id':1}])
def test_fingerprint_binds_every_original_snapshot_field(changes):
    snapshot=opening()
    assert replace(snapshot,**changes).fingerprint!=snapshot.fingerprint


@pytest.mark.parametrize('record',[None,row(user_id=uuid.UUID(int=3)),row(committed_message_id=7)])
def test_foreign_missing_or_used_opening_is_not_captured(record):
    # Decoding eligibility is checked by chat before invoking capture. This
    # helper never reads ciphertext or guesses a different opening.
    assert g.capture(record,user_id=UID,decoded_text=None) is None


def test_no_new_ttl_and_nullable_legacy_issue_time_are_introduced():
    snapshot=g.capture(row(created_at=None,activity_date=date(2020,1,1)),user_id=UID,decoded_text='Legacy valid opening')
    assert snapshot.issued_at is None and snapshot.activity_date==date(2020,1,1)
    assert g.attach([{'role':'user','content':'reply'}],snapshot,user_id=UID)[0]['content']==snapshot.text


def test_none_preserves_conversation_and_current_user_identity_exactly():
    messages=history()
    assert g.attach(messages,None,user_id=UID) is messages
    assert g.guard_history(messages,None,user_id=UID) is messages


@pytest.mark.parametrize('snapshot',[replace(opening(),owner_id=uuid.UUID(int=3)),replace(opening(),committed_id=7)])
def test_foreign_or_committed_snapshot_cannot_be_attached(snapshot):
    messages=history()
    before=list(messages)
    with pytest.raises(g.OpeningContextError):
        g.attach(messages,snapshot,user_id=UID)
    with pytest.raises(g.OpeningContextError):
        g.guard_history([],snapshot,user_id=UID)
    assert messages==before


def test_attach_is_pure_once_and_preserves_historical_sources_and_current_object():
    messages=history()
    before=[dict(m) for m in messages]
    snapshot=opening()
    result=g.attach(messages,snapshot,user_id=UID)
    assert result is not messages and result[-1] is messages[-1]
    assert all(result[i] is messages[i] for i in range(3))
    assert result[0].source_ref is messages[0].source_ref
    assert result[-2].source_ref is None and set(result[-2])=={'role','content'}
    assert g.attach(result,snapshot,user_id=UID) is result
    copied=[item.copy() for item in result]
    assert g.attach(copied,snapshot,user_id=UID) is copied
    assert messages==before
    assert_adjacent(result,snapshot.text,messages[-1]['content'])
    assert 'opening_fingerprint' not in json.dumps(result)
    assert str(GID) not in json.dumps(result)


def test_matching_historical_text_is_never_removed_by_text_deduplication():
    snapshot=opening()
    old=source(1,'assistant',snapshot.text)
    messages=[old,HistoryEntry('user','current')]
    result=g.attach(messages,snapshot,user_id=UID)
    assert result[0] is old and result[0].source_ref is not None
    assert result[-2] is not old and result[-2].source_ref is None


def test_reassembly_repositions_only_the_same_local_opening_entry():
    snapshot=opening()
    original=g.attach(history(),snapshot,user_id=UID)
    entry=original[-2]
    volatile=HistoryEntry('system','Additional volatile context')
    interleaved=[*original[:-1],volatile,original[-1]]
    result=g.attach(interleaved,snapshot,user_id=UID)
    assert result[-3] is volatile and result[-2] is entry and result[-1] is original[-1]
    with pytest.raises(g.OpeningContextError,match='opening_context_conflict'):
        g.attach(result,replace(snapshot,text='different opening'),user_id=UID)


@pytest.mark.parametrize('messages',[[],[HistoryEntry('assistant','not current')],
    [source(1,'user','historical user')]])
def test_opening_requires_an_uncheckpointed_current_user(messages):
    with pytest.raises(g.OpeningContextError,match='opening_current_user_required'):
        g.attach(messages,opening(),user_id=UID)


@pytest.mark.parametrize('language,fragments',[
    ('ko',['짧은 답','이전 주제를 명시','새 주제']),
    ('en',['short reply','earlier subject','new subject']),
    ('ja',['短い返事','以前の話題','新しい話題']),
])
def test_reference_note_is_equivalent_and_never_duplicates_the_opening(language,fragments):
    note=g.reference_note(language)
    assert all(fragment in note for fragment in fragments)
    assert opening().text not in note
    assert g.reference_note(language+'-'+language.upper())==note


@pytest.mark.parametrize('language,fragments',[
    ('ko',['현재 대화에 실제 남아','캐릭터로서 네가 한 말','상대의 행동으로 뒤바꾸거나','원문에 없는 장소','모르는 세부','원문이 현재 대화에 없으면','이전 주제나 새 주제를 명시']),
    ('en',['actually present in the current conversation','You said it as the character',"not the user's statement",'Do not reverse the action','Do not invent places','details you do not know','original opening is absent','earlier or new subject']),
    ('ja',['現在の会話に実際に残っている','キャラクターとしてきみが言った','相手が言ったことではない','相手の行動にすり替えたり','原文にない場所','分からない細部','原文が現在の会話になければ','以前の話題や新しい話題を明示']),
])
def test_identity_note_conditions_speaker_continuity_on_present_original(language,fragments):
    note=g.identity_note(language)
    assert all(fragment in note for fragment in fragments)
    assert opening().text not in note
    assert g.identity_note(language+'-'+language.upper())==note
    assert g.reference_note(language)!=note


@pytest.mark.parametrize('language,displayed,current',[
    ('ko','방금 산책을 다녀왔어.','나도.'),
    ('ko','방금 밥을 먹었어.','어제 메뉴 얘기로 돌아갈래.'),
    ('ko','방금 산책을 다녀왔어.','다른 얘긴데 오늘 일 때문에 바빴어.'),
    ('en','I went for a walk just now','me too'),
    ('en','I just had a meal',"I mean yesterday's menu instead"),
    ('en','I went for a walk just now','Separate topic: work today'),
    ('ja','さっき散歩に行ってきたよ。','私も。'),
    ('ja','さっきご飯を食べたよ。','昨日の献立の話に戻りたい。'),
    ('ja','さっき散歩に行ってきたよ。','別の話だけど、今日は仕事で忙しかった。'),
])
async def test_actual_direct_provider_payload_has_current_opening_adjacent_and_preserves_explicit_input(monkeypatch,language,displayed,current):
    snapshot=opening(displayed)
    messages=history(current)
    messages[-2]['content']+='\n'+g.reference_note(language)+'\n'+g.identity_note(language)
    arranged=g.attach(messages,snapshot,user_id=UID)
    create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='synthetic reply'),finish_reason='stop')],usage=None,model='synthetic',id='synthetic'))
    monkeypatch.setattr(llm,'_get_openai_client',lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    await llm._generate_openai(['Stable persona'],arranged,model='gpt-6-luna',timeout=1)
    sent=create.await_args.kwargs['messages']
    assert_adjacent(sent,snapshot.text,current)
    assert sum(g.identity_note(language) in (m.get('content') or '') for m in sent)==1
    assert 'Covered old breakfast' in [m['content'] for m in sent]
    assert all(set(m)=={'role','content'} for m in sent)
    # The typed agent serializer must agree on role/order, not invent a system lead.
    typed=llm.to_openai_messages(['Stable persona'],runtime._transcript(arranged))
    assert typed==sent


async def test_agent_two_hops_and_coverage_pruning_keep_mandatory_opening(monkeypatch):
    snapshot=opening('I just had dinner')
    messages=history('What did you eat?')
    messages[-2]['content']+='\n'+g.identity_note('en')
    arranged=g.attach(messages,snapshot,user_id=UID)
    coverage=VerifiedCoverage(UID,0,uuid.UUID(int=9),2,'a'*64).bind_sources(m.source_ref for m in messages[:2])
    pruned=without_covered(arranged,coverage)
    assert pruned[-2] is arranged[-2] and pruned[-1] is messages[-1]
    def budget(system,wire,**kwargs):
        if any('Covered old' in (item.get('content') or '') for item in wire):
            raise context_retention.RetentionError('context_input_budget')
    monkeypatch.setattr(context_retention,'assert_input_budget',budget)
    fake=_FakeSteps(_calls_step(_call(1)),_text_step('synthetic final',purpose='tool_final'))
    monkeypatch.setattr(llm,'generate_step',fake)
    tool=_FakeTool(data={'items':['Synthetic tool result']})
    await runtime.run_turn(['Stable persona'],arranged,config=_cfg(),user_id=UID,language='en',
        activity_date=DAY,user_text=messages[-1]['content'],registry=_FakeRegistry(tool),
        session_factory=_FakeToolSession,retention_enabled=True,retention_coverage=coverage)
    assert len(fake.calls)==2 and tool.ran==1
    for call in fake.calls:
        sent=llm.to_openai_messages(['Stable persona'],call['transcript'])
        assert_adjacent(sent,snapshot.text,messages[-1]['content'])
        assert sum(g.identity_note('en') in (m.get('content') or '') for m in sent)==1
        assert not any('Covered old' in (m.get('content') or '') for m in sent)
    final=llm.to_openai_messages([],fake.calls[-1]['transcript'])
    assert any(m['role']=='tool' for m in final)


@pytest.mark.parametrize('language',['ko','en','ja'])
def test_identity_note_does_not_reconstruct_absent_or_pruned_original(language):
    current=HistoryEntry('user','Explicit different subject')
    messages=[HistoryEntry('system',g.reference_note(language)+'\n'+g.identity_note(language)),current]
    assert g.attach(messages,None,user_id=UID) is messages
    wire=llm.to_openai_messages([],runtime._transcript(messages))
    assert not any(m['role']=='assistant' for m in wire)
    assert not any(opening().text in m['content'] for m in wire)
    assert wire[-1]==dict(current)


def test_guard_opening_is_separate_virtual_context_and_never_mutates_db_proof():
    raw=[crisis_guard.GuardMessage('user' if i%2 else 'assistant','Raw '+str(i),
          'safety_check' if i==6 else 'normal',i) for i in range(1,7)]
    proof=tuple((m.message_id,m.role,m.kind,m.content) for m in raw)
    assert crisis_guard.latest_intervention(raw)=='safety_check'
    screened=g.guard_history(raw,opening(),user_id=UID)
    assert len(screened)==6 and screened[:-1]==raw[-5:]
    assert screened[-1].role=='assistant' and screened[-1].kind=='greeting' and screened[-1].message_id is None
    assert not crisis_guard.prepare_input('short current reply',screened)[1]
    assert proof==tuple((m.message_id,m.role,m.kind,m.content) for m in raw)
    assert screened is not raw


@pytest.mark.parametrize('limit',[1,6])
def test_guard_context_remains_bounded(limit):
    raw=[crisis_guard.GuardMessage('user','raw') for _ in range(9)]
    assert len(g.guard_history(raw,opening(),user_id=UID,history_limit=limit))==limit
