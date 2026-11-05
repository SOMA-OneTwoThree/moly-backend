"""Current concern policy; responses mocked, no live models or database."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from app.services import crisis_guard as guard


def decision(case='self_thought', answer='same_unresolved', asked=None):
    def choice(name, selected, values):
        return {'name':name,'type':'choice','choice':selected,
                'probabilities':[{'value':value,'probability':float(value==selected)} for value in values]}
    answers=[choice('case',case,guard.CASES),choice('followup',answer,guard.ANSWERS),
             {'name':'method_request','type':'predicate','probability':0.0}]
    if asked is not None:
        answers.append({'name':'same_concern_asked','type':'predicate','probability':float(asked)})
    return {'answers':answers}


@pytest.mark.parametrize('case', ['self_thought','meaning_unclear'])
@pytest.mark.parametrize('asked', [False,True])
@pytest.mark.parametrize('answer', guard.ANSWERS)
def test_guide_is_not_blanket_suppression_and_semantic_question_history_controls_reask(case, asked, answer):
    result=guard.parse_decision(decision(case,answer,asked),last_intervention='safety',recheck_available=True)
    expected=('safety' if answer=='same_confirmed' else 'normal' if answer=='same_denied'
              else 'ask' if not asked else 'normal')
    assert result.route==expected and result.status=='ok'
    assert result.same_concern_asked is asked and result.question_history_status=='ok'


@pytest.mark.parametrize('case', guard.ACTION_CASES)
@pytest.mark.parametrize('fault', ['missing','refusal','bad'])
def test_question_history_failure_does_not_lower_current_action(case, fault):
    payload=decision(case)
    if fault=='refusal':
        payload['answers'].append({'name':'same_concern_asked','type':'refusal'})
    elif fault=='bad':
        payload['answers'].append({'name':'same_concern_asked','type':'predicate','probability':float('nan')})
    result=guard.parse_decision(payload,last_intervention='safety',recheck_available=True)
    assert result.route=='safety' and result.question_history_status in {'invalid','refusal'}


@pytest.mark.parametrize('answer', ['same_confirmed','same_unresolved','different_topic'])
def test_auxiliary_history_failure_preserves_confirmation_but_cannot_assert_new_question(answer):
    result=guard.parse_decision(decision(answer=answer),last_intervention='safety',recheck_available=True)
    assert result.status=='invalid' and result.same_concern_asked is None
    assert result.route==('safety' if answer=='same_confirmed' else 'normal')


def test_guide_without_any_question_allows_first_clarification():
    result=guard.parse_decision(decision('meaning_unclear'),last_intervention='safety')
    assert result.route=='ask' and result.status=='ok' and result.same_concern_asked is False


def test_same_concern_unresolved_after_actual_question_does_not_ask_again():
    result=guard.parse_decision(decision('meaning_unclear'),last_intervention='safety_check')
    assert result.route=='normal' and result.same_concern_asked is True


@pytest.mark.parametrize('kind', ['safety','safety_check'])
def test_topic_change_ordinary_stays_normal_and_a_new_unclear_concern_can_be_asked(kind):
    assert guard.parse_decision(decision('not_applicable','different_topic'),last_intervention=kind).route=='normal'
    assert guard.parse_decision(decision('meaning_unclear','different_topic'),last_intervention=kind).route=='ask'


def pair():
    return (guard.GuardMessage('user','synthetic old current thought','safety_check',1),
            guard.GuardMessage('assistant','synthetic clarification','safety_check',2))


async def test_question_outside_last_six_is_exact_bounded_semantic_evidence_with_real_guide_anchor():
    evidence=pair()
    recent=[guard.GuardMessage('user','synthetic ongoing situation',message_id=i) for i in range(3,8)]
    recent.append(guard.GuardMessage('assistant','synthetic guide','safety',8))
    calls=[]
    async def respond(request):
        body=json.loads(request.content)
        calls.append(body)
        assert len(body['input'])==9 and body['input'][0]['content']==evidence[0].content
        questions={item['name']:item for item in body['questions']}
        assert 'assistant guide is input index 7' in questions['followup']['instructions']
        assert 'rechecks are input indexes [1]' in questions['same_concern_asked']['instructions']
        return httpx.Response(200,json=decision('meaning_unclear','same_unresolved',True))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result=await guard.decide('synthetic unclear continuation',recent,recheck_evidence=evidence,
            api_key='synthetic-key',deadline=time.monotonic()+2,client=client)
    assert len(calls)==1 and result.route=='normal' and result.last_intervention=='safety'


def test_visible_pair_deduplicates_only_same_identity_not_equal_text():
    evidence=pair()
    recent=[*evidence,guard.GuardMessage('user',evidence[0].content,'normal',3)]
    inputs,followup=guard.prepare_input('synthetic current',recent,recheck_evidence=evidence)
    assert followup and len(inputs)==4
    assert inputs[0]['content']==inputs[2]['content']


@pytest.mark.parametrize('evidence', [
    (guard.GuardMessage('user','old'),guard.GuardMessage('assistant','q','safety_check')),
    (guard.GuardMessage('user','old','normal',2),guard.GuardMessage('assistant','q','safety_check',1)),
    (guard.GuardMessage('user','old','normal',1),guard.GuardMessage('assistant','guide','safety',2)),
])
def test_bad_historical_question_proof_is_rejected(evidence):
    with pytest.raises(ValueError):
        guard.prepare_input('current',[],recheck_evidence=evidence)


def test_changed_historical_same_id_cannot_be_silently_replaced():
    with pytest.raises(ValueError,match='conflicting recheck evidence'):
        guard.prepare_input('current',[guard.GuardMessage('user','changed','safety_check',1)],recheck_evidence=pair())


@pytest.mark.parametrize(('language','text'), [('ko','죽고 싶다고'),('en','I said I want to die'),('ja','死にたいって言ってる')])
@pytest.mark.parametrize('anchor', ['safety_check','safety'])
async def test_direct_reassertion_is_confirmed_after_question_or_guide_protocol(language,text,anchor):
    async def respond(request):
        body=json.loads(request.content)
        assert body['input'][-1]['content']==text
        question=next(item for item in body['questions'] if item['name']=='followup')
        assert f'assistant {"recheck" if anchor=="safety_check" else "guide"} is input index 1' in question['instructions']
        assert text in question['instructions'] and 'This is not a bare acknowledgment' in question['instructions']
        return httpx.Response(200,json=decision('self_thought','same_confirmed'))
    recent=[guard.GuardMessage('user','synthetic current thought'),guard.GuardMessage('assistant','synthetic intervention',anchor)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result=await guard.decide(text,recent,api_key='synthetic-key',deadline=time.monotonic()+2,client=client)
    assert result.route=='safety' and guard.fixed_reply(language)


def test_positive_repetition_does_not_weaken_ack_denial_and_past_distinctions():
    instructions=guard.questions(followup=True,anchor_kind='safety')[1]['instructions']
    for rule in ['bare yes without that confirmation is same_unresolved',
                 'resolved past episode with no current thoughts is same_denied',
                 'Topic changes are different_topic', 'a prior guide does not make current serious thoughts normal']:
        assert rule in instructions


@pytest.mark.parametrize(('old', 'current', 'answer', 'expected'), [
    ('시험 망해서 죽고 싶어',
     '시험 얘기는 농담이었고 그때 생각은 없어. 지금은 별개로 이별 때문에 새로 사라지고 싶어.',
     'different_topic', 'ask'),
    ('I want to die because I failed my exam',
     'The exam thing was a joke and that concern is over. Separately, after a breakup, I now have new thoughts of disappearing.',
     'different_topic', 'ask'),
    ('試験に落ちて死にたい',
     '試験の話は冗談で、その時の気持ちはもうない。別の話だけど、失恋して今は新しく消えたいと思っている。',
     'different_topic', 'ask'),
    ('시험 망해서 죽고 싶어',
     '아까부터 같은 생각이 계속된다고. 농담이 아니고 이별한 일까지 겹쳐서 더 죽고 싶어.',
     'same_confirmed', 'safety'),
    ('I want to die because I failed my exam',
     'Those same thoughts have continued since then. It is not a joke; the breakup is another reason I still want to die.',
     'same_confirmed', 'safety'),
    ('試験に落ちて死にたい',
     'さっきから同じ気持ちが続いている。冗談じゃないし、失恋も重なって今も死にたい。',
     'same_confirmed', 'safety'),
])
@pytest.mark.parametrize('anchor', ['safety_check', 'safety'])
async def test_mixed_old_denial_new_concern_priority_and_ongoing_repetition_protocol(old, current, answer, expected, anchor):
    """Wire/route contract only; live classification quality is evaluated separately."""
    calls = []

    async def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body['input'][-1]['content'] == current
        question = next(item for item in body['questions'] if item['name'] == 'followup')
        instructions = question['instructions']
        assert 'choose different_topic rather than same_denied' in instructions
        assert 'classify that new concern in case on its own merits' in instructions
        assert 'A changed reason alone does not establish a new concern' in instructions
        assert 'remains same_confirmed' in instructions
        choices = {item['value']: item['description'] for item in question['choices']}
        assert 'choose different_topic instead of same_denied' in choices['same_denied']
        assert 'even if the message also denies or retracts the old concern' in choices['different_topic']
        return httpx.Response(200, json=decision('self_thought', answer))

    recent = [guard.GuardMessage('user', old, message_id=1),
              guard.GuardMessage('assistant', 'synthetic intervention', anchor, 2)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await guard.decide(current, recent, api_key='synthetic-key',
                                    deadline=time.monotonic()+2, client=client)
    assert len(calls) == 1 and result.route == expected and result.status == 'ok'


@pytest.mark.parametrize('case', ['self_thought', 'meaning_unclear'])
@pytest.mark.parametrize('asked', [True, False, None])
def test_different_guide_topic_still_respects_actual_same_concern_question(case, asked):
    expected = 'ask' if asked is False else 'normal'
    assert guard.choose_route(case, 'different_topic', last_intervention='safety',
                              same_concern_asked=asked) == expected
    for action in guard.ACTION_CASES:
        assert guard.choose_route(action, 'different_topic', last_intervention='safety',
                                  same_concern_asked=asked) == 'safety'
    assert guard.choose_route(case, 'same_confirmed', last_intervention='safety',
                              same_concern_asked=asked) == 'safety'


@pytest.mark.parametrize(('old', 'current'), [
    ('아직 죽고 싶다는 뜻인지 모르겠어. 내 별개의 고민이야.',
     '아까 죽고 싶다는 뜻인지 모르겠다고 한 그 별개의 고민이야. 여전히 내 뜻이 뭔지 모르겠어.'),
    ('I do not know whether I mean wanting to die. This is my separate concern.',
     'I mean that separate concern when I did not know whether I meant wanting to die. I still cannot tell.'),
    ('死にたいという意味かまだ分からない。自分の別の悩みだよ。',
     'さっき死にたいという意味か分からないと言った別の悩みだよ。今も自分の意味が分からない。'),
])
async def test_old_question_b_new_guide_d_then_unclear_b_keeps_exact_question_dedup_wire(old, current):
    evidence = (guard.GuardMessage('user', old, message_id=1),
                guard.GuardMessage('assistant', 'synthetic B clarification', 'safety_check', 2))
    recent = [guard.GuardMessage('user', 'synthetic ordinary topic', message_id=i) for i in range(3, 7)]
    recent += [guard.GuardMessage('user', 'synthetic distinct D current action', message_id=7),
               guard.GuardMessage('assistant', 'synthetic D guide', 'safety', 8)]

    async def respond(request):
        body = json.loads(request.content)
        questions = {item['name']: item for item in body['questions']}
        assert 'assistant guide is input index 7' in questions['followup']['instructions']
        assert 'rechecks are input indexes [1]' in questions['same_concern_asked']['instructions']
        assert body['input'][-1]['content'] == current
        return httpx.Response(200, json=decision('meaning_unclear', 'different_topic', True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await guard.decide(current, recent, recheck_evidence=evidence, api_key='synthetic-key',
                                    deadline=time.monotonic()+2, client=client)
    assert result.route == 'normal' and result.same_concern_asked is True
