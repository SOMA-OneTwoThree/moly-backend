"""Immediate recheck only. All Decisions HTTP is mocked; no model-quality claims."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from app.services import crisis_guard as guard


def decision(case='self_thought', answer=None):
    def choice(name, selected, values):
        return {'name':name,'type':'choice','choice':selected,
                'probabilities':[{'value':value,'probability':float(value==selected)} for value in values]}
    answers=[choice('case',case,guard.CASES)]
    if answer is not None:
        answers.append(choice('followup',answer,guard.ANSWERS))
    answers.append({'name':'method_request','type':'predicate','probability':0.0})
    return {'answers':answers}


@pytest.mark.parametrize('kind', ['normal','safety','safety_check','fortune'])
@pytest.mark.parametrize('case', guard.CASES)
@pytest.mark.parametrize('answer', guard.ANSWERS)
def test_latest_reply_only_policy_for_all_cases(kind,case,answer):
    observed=kind if kind in guard.SAFETY_KINDS else None
    parsed=guard.parse_decision(decision(case,answer if kind=='safety_check' else None),last_intervention=observed)
    expected=('safety' if case in guard.ACTION_CASES else 'normal' if case=='not_applicable'
              else 'ask' if kind!='safety_check' else 'safety' if answer=='same_confirmed' else 'normal')
    assert parsed.route==expected
    assert parsed.followup is (kind=='safety_check')
    assert parsed.same_concern_asked is None and parsed.question_history_status is None


@pytest.mark.parametrize('latest', ['normal','safety','fortune'])
@pytest.mark.parametrize('earlier', ['safety','safety_check'])
def test_earlier_intervention_never_enables_followup_or_suppresses_first_question(latest,earlier):
    history=[guard.GuardMessage('user','older concern'),guard.GuardMessage('assistant','older response',earlier),
             guard.GuardMessage('user','newer topic'),guard.GuardMessage('assistant','last actual response',latest)]
    _,followup=guard.prepare_input('synthetic current unclear meaning',history)
    assert followup is False
    observed=guard.latest_intervention(history)
    assert observed==('safety' if latest=='safety' else None)
    assert guard.parse_decision(decision('meaning_unclear'),last_intervention=observed).route=='ask'


@pytest.mark.parametrize('history', [[],[guard.GuardMessage('user','old raw')]])
def test_no_previous_assistant_means_no_followup(history):
    _,followup=guard.prepare_input('current',history)
    assert followup is False and guard.latest_intervention(history) is None
    assert guard.parse_decision(decision()).route=='ask'


@pytest.mark.parametrize('answer', ['same_denied','same_unresolved','different_topic'])
@pytest.mark.parametrize('case', ['self_thought','meaning_unclear'])
def test_immediate_recheck_is_not_repeated_even_on_unclear_or_new_topic(case,answer):
    parsed=guard.parse_decision(decision(case,answer),last_intervention='safety_check')
    assert parsed.route=='normal' and parsed.status=='ok'


@pytest.mark.parametrize('case', guard.ACTION_CASES)
@pytest.mark.parametrize('failure', ['missing','refusal','invalid'])
def test_current_action_wins_over_immediate_followup_failure(case,failure):
    payload=decision(case,'same_confirmed')
    item=payload['answers'][1]
    if failure=='missing':
        payload['answers'].remove(item)
    elif failure=='refusal':
        item['type']='refusal'
    else:
        item['choice']='same_unresolved_asked'
    parsed=guard.parse_decision(payload,last_intervention='safety_check')
    assert parsed.route=='safety' and parsed.status==('refusal' if failure=='refusal' else 'invalid')


@pytest.mark.parametrize('old', ['same_concern_asked','same_unresolved_asked','same_unresolved_unasked'])
def test_removed_long_term_history_protocol_is_rejected(old):
    payload=decision('self_thought','same_confirmed')
    if old=='same_concern_asked':
        payload['answers'].append({'name':old,'type':'predicate','probability':1.0})
    else:
        payload['answers'][1]['choice']=old
    parsed=guard.parse_decision(payload,last_intervention='safety_check')
    assert parsed.status=='invalid' and parsed.route=='normal'


async def test_exact_recent_six_only_and_legacy_history_attribute_is_ignored():
    class History(list):
        recheck_evidence=(guard.GuardMessage('user','must not be injected',message_id=1),
                          guard.GuardMessage('assistant','must not be injected','safety_check',2))
    recent=History([guard.GuardMessage('user' if i%2 else 'assistant','synthetic '+str(i),
                                     'safety_check' if i==2 else 'normal',i) for i in range(1,9)])
    calls=[]
    async def respond(request):
        body=json.loads(request.content)
        calls.append(body)
        assert body['input']==[{'role':m.role,'content':m.content} for m in recent[-6:]]+[
            {'role':'user','content':'synthetic current'}]
        assert [q['name'] for q in body['questions']]==['case','method_request']
        return httpx.Response(200,json=decision('meaning_unclear'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        parsed=await guard.decide('synthetic current',recent,api_key='synthetic-key',
            deadline=time.monotonic()+2,client=client)
    assert len(calls)==1 and parsed.route=='ask' and parsed.last_intervention is None


@pytest.mark.parametrize('language,text', [('ko','죽고 싶다고'),('en','I said I want to die'),('ja','死にたいって言ってる')])
async def test_direct_current_repetition_after_immediate_question_is_confirmed_protocol(language,text):
    async def respond(request):
        body=json.loads(request.content)
        assert [q['name'] for q in body['questions']]==['case','followup','method_request']
        instructions=body['questions'][1]['instructions']
        assert 'immediately previous assistant recheck is input index 1' in instructions
        assert 'actual preceding user message is input index 0' in instructions
        assert text in instructions and 'This is not a bare acknowledgment' in instructions
        return httpx.Response(200,json=decision('self_thought','same_confirmed'))
    recent=[guard.GuardMessage('user','synthetic current thought'),
            guard.GuardMessage('assistant',guard.reask_fallback(language),'safety_check')]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        parsed=await guard.decide(text,recent,api_key='synthetic-key',deadline=time.monotonic()+2,client=client)
    assert parsed.route=='safety' and parsed.followup and guard.fixed_reply(language)


@pytest.mark.parametrize('language', ['ko','en','ja'])
async def test_thought_after_guide_starts_case_routing_again(language):
    async def respond(request):
        body=json.loads(request.content)
        assert [q['name'] for q in body['questions']]==['case','method_request']
        return httpx.Response(200,json=decision('self_thought'))
    recent=[guard.GuardMessage('user','synthetic earlier thought'),
            guard.GuardMessage('assistant',guard.fixed_reply(language),'safety')]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        parsed=await guard.decide('synthetic current thought',recent,api_key='synthetic-key',
            deadline=time.monotonic()+2,client=client)
    assert parsed.route=='ask' and not parsed.followup and parsed.last_intervention=='safety'


def test_only_immediate_question_anchor_is_declared_and_unknown_source_is_not_invented():
    questions=guard.questions(followup=True,anchor_index=0)
    prompt=questions[1]['instructions']
    assert 'source user message is not visible' in prompt
    assert 'do not substitute an older concern' in prompt
    assert 'whether the words mean' in prompt and 'not confirmation' in prompt
    assert 'Do not link an older question or guide' in prompt
    assert 'same_concern_asked' not in json.dumps(questions)
