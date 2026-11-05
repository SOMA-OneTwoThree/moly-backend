"""Prompt scope/protocol checks; actual classification quality is evaluated separately."""
import json
import time

import httpx
import pytest

from app.services import crisis_guard as g
from tests.test_crisis_concern_policy import decision


def test_meaning_uncertainty_is_not_an_affirmative_thought_or_confirmation():
    cases = {item['value']: item['description'] for item in g._CASE_CHOICES}
    answers = {item['value']: item['description'] for item in g._FOLLOWUP_CHOICES}
    assert "Questioning whether one's current words" in cases['self_thought']
    assert "uncertainty about whether one's current expression" in cases['meaning_unclear']
    assert 'meaning is still uncertain' in answers['same_confirmed']
    assert 'neither confirmation nor a denial' in answers['same_unresolved']
    assert 'uncertainty about what to do while affirming the wish' in g.questions(followup=True)[1]['instructions']


def test_episode_link_precedes_inherited_action_without_erasing_actual_current_action():
    prompt = g._CASE_INSTRUCTIONS
    assert prompt.index('Before carrying forward an earlier plan or action') < prompt.index('Keep self_intent_plan')
    assert 'a separate current concern whose self-harm meaning is unresolved' in prompt
    assert 'Explicit current self-directed intent, preparation or action takes precedence' in prompt
    assert g.choose_route('self_preparation_action', 'same_unresolved', last_intervention='safety_check') == 'safety'


@pytest.mark.parametrize('followup', [False, True])
async def test_boundary_instructions_reach_same_single_request_without_new_questions(followup):
    calls = []
    async def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert [q['name'] for q in payload['questions']] == (['case', 'followup', 'method_request']
            if followup else ['case', 'method_request'])
        assert 'Before carrying forward an earlier plan or action' in payload['questions'][0]['instructions']
        if followup:
            assert 'remains same_unresolved even when those words are repeated' in payload['questions'][1]['instructions']
        return httpx.Response(200, json=decision('meaning_unclear', 'same_unresolved' if followup else None))
    recent = [g.GuardMessage('user', 'synthetic ambiguous expression'),
              g.GuardMessage('assistant', 'synthetic clarification' if followup else 'synthetic guide',
                             'safety_check' if followup else 'safety')]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await g.decide('synthetic uncertainty', recent, api_key='synthetic-only',
            deadline=time.monotonic() + 2, client=client)
    assert len(calls) == 1 and result.route == ('normal' if followup else 'ask')
