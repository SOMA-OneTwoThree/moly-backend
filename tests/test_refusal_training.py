"""Approved refusal manuscript: continuity, private rubric, and plain refusals."""
import json
from pathlib import Path

import pytest

from app.services import training_catalog, training_grading

COURSE = training_catalog.TrainingCatalog.load().get('refusal-practice-v1')
WRITE = {3, 6, 9, 12, 16, 18, 21, 23, 24, 26, 27, 28, 29, 30}


def test_approved_course_structure_and_exact_rewards():
    assert [s.number for s in COURSE.stages] == list(range(1, 31))
    assert {s.number for s in COURSE.stages if s.answer_type == 'write'} == WRITE
    assert {s.number: s.reward_hay for s in COURSE.stages if s.review_point} == {
        5: 30, 15: 60, 30: 150,
    }
    assert {s.number for s in COURSE.stages if not any(line.slot for line in s.lines)} == {5, 13, 19}
    assert all(s.layout == 'chat' for s in COURSE.stages)


@pytest.mark.parametrize('language', ['ko', 'en', 'ja'])
def test_linked_dialogue_localization_and_private_grading(language):
    chosen = next(o for o in COURSE.stage(14).options if o.correct)
    assert COURSE.stage(15).lines[2].text.pick(language) == chosen.text.pick(language)
    for s in COURSE.stages:
        public = training_catalog.public_stage(s, language)
        assert not {'grading', 'explanation', 'example'} & public.keys()
        assert all(set(o) == {'id', 'text'} for o in public['options'])
        if s.number in WRITE:
            assert s.explanation.pick(language)
            assert s.grading.example is None
            assert s.grading.retry_hint.pick(language)
            assert s.grading.topic == 'refusal'
        else:
            for o in s.options:
                verdict = training_grading.grade_pick(s, o.id, language)
                assert verdict.passed == o.correct
                assert bool(verdict.hint) != o.correct


def test_original_situations_and_approved_editorial_corrections():
    assert '쉬는 시간' in COURSE.stage(11).options[1].text.ko
    assert '밖에서' not in COURSE.stage(11).options[1].text.ko
    assert '멀미' not in COURSE.stage(4).options[0].text.ko
    assert '디자인' not in COURSE.stage(20).lines[0].text.ko
    assert '수 있다' in COURSE.stage(19).options[1].text.ko
    for s in COURSE.stages:
        assert '경계' not in s.mission.ko
        assert '사용자가 직접 입력' not in str(training_catalog.public_stage(s, 'ko'))


@pytest.mark.parametrize(('language', 'answer'), [('ko', '싫어'), ('ko', '안 빌려줄래'), ('en', 'No.'), ('ja', '嫌だ')])
def test_short_clear_refusals_reach_the_judge(language, answer):
    assert training_grading.check_rules(COURSE.stage(26), answer, language) is None


def test_refusal_prompt_does_not_use_existing_courses_scoring():
    prompt = training_grading.system_prompt(COURSE.stage(26), 'ko')
    assert 'PASS a contextually meaningful refusal' in prompt
    assert 'Never require apologies' in prompt
    assert 'a small upside' not in prompt
    old = training_catalog.TrainingCatalog.load().get('positive-circuit-v2').stage(3)
    assert 'a small upside' in training_grading.system_prompt(old, 'ko')


def test_manuscript_source_uses_independent_course_id():
    # Existing progress remains associated with its own course ID.
    source = json.loads((Path(__file__).parents[1] / 'content/trainings/refusal.json').read_text())
    assert source['id'] == COURSE.id
    assert len(source['stages']) == 30
