"""Explicit live grading evaluation with synthetic answers only; no user records or usage DB writes.

uv run python -m scripts.evaluate_training_grading --output /path/to/report.json
This calls the configured model. A timeout is an evaluation error, never a passing test.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

from app.config import settings
from app.services import llm, training_grading
from app.services.training_catalog import TrainingCatalog


async def evaluate(languages=('ko',), repeats=1, suite='all', only=None):
    cases = []
    catalog = TrainingCatalog.load()
    for course in catalog.trainings:
        if course.archived:
            continue
        for stage in course.stages:
            if stage.answer_type == 'write':
                for language in languages:
                    if stage.grading.example:
                        cases.append((course.id, stage, 'example', stage.grading.example.pick(language), True, language))
                    attacks = {'ko': '나는 뭘 해도 못하는 한심한 인간이야.',
                               'en': 'I am a worthless person who cannot do anything right.',
                               'ja': '私は何をしてもだめな、価値のない人間だ。'}
                    cases.append((course.id, stage, 'self_attack', attacks[language], False, language))
    for number, answer in [(3, '다음부터 시간 약속을 잘 지키겠습니다.'), (14, '제 잘못입니다. 죄송합니다.')]:
        cases.append(('positive-circuit-v2', catalog.get('positive-circuit-v2').stage(number), 'off_mission_apology_or_plan', answer, False, 'ko'))
    for answer, expected in [('뭐래', False), ('ㅋㅋㅋ', False), ('밥이 마싯어서 기분조앗다', True),
                             ('기다리는동안 친구랑 얘기해서 조앗다', True)]:
        cases.append(('positive-circuit-v2', catalog.get('positive-circuit-v2').stage(3),
                      'qa_reaction_or_typo', answer, expected, 'ko'))
    if suite in ('natural', 'heldout'):
        cases = []
    if suite != 'baseline':
        filename = 'grading-eval-holdout.json' if suite == 'heldout' else 'grading-eval.json'
        natural = json.loads((Path(__file__).parents[1] / 'content/trainings' / filename).read_text())
        for case in natural:
            for language, answer in case['answers'].items():
                if language in languages:
                    cases.append((case['course'], catalog.get(case['course']).stage(case['stage']),
                                  case['kind'], answer, case['expected'], language))
    if only:
        cases = [case for case in cases if case[0] == only or f'{case[0]}:{case[1].number}' == only]
        if not cases:
            raise ValueError(f'No cases for {only}')
    gate = asyncio.Semaphore(3)
    completed = 0
    async def one(tid, stage, kind, answer, expected, language):
        nonlocal completed
        row = {'course': tid, 'stage': stage.number, 'kind': kind, 'expected': expected,
               'language': language, 'answer': answer,
               'prompt_version': 'refusal-grade-v1' if stage.grading.topic == 'refusal' else training_grading.PROMPT_VERSION}
        async with gate:
            try:
                result = await llm.generate(
                    training_grading.system_prompt(stage, language),
                    [{'role': 'user', 'content': f'<answer>{answer}</answer>'}],
                    model=settings.model_training_grade, reasoning_effort='none', max_tokens=200,
                    foreground_deadline=time.monotonic() + training_grading.AI_DEADLINE_S,
                )
                parsed = training_grading._parse(result.text)
                row['actual'] = parsed[0] if parsed else None
                row['hint'] = parsed[1] if parsed else None
                row['ok'] = parsed is not None and parsed[0] == expected
                row['input_tokens'] = result.input_tokens
                row['output_tokens'] = result.output_tokens
                row['cache_read_tokens'] = result.cache_read_tokens
            except Exception as exc:
                row.update(error=type(exc).__name__, ok=False)
            completed += 1
            if completed % 25 == 0:
                print(f'Evaluated {completed}/{len(cases) * repeats}', flush=True)
        return row
    rows = await asyncio.gather(*(one(*case) for _ in range(repeats) for case in cases))
    return {'model': settings.model_training_grade, 'prompt_versions': sorted({row['prompt_version'] for row in rows}),
            'total': len(rows), 'passed': sum(row['ok'] for row in rows), 'results': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--languages', nargs='+', choices=['ko', 'en', 'ja'], default=['ko'])
    parser.add_argument('--repeats', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--suite', choices=['all', 'baseline', 'natural', 'heldout'], default='all')
    parser.add_argument('--only', help='Evaluate one course:stage after a focused rubric correction')
    args = parser.parse_args()
    report = asyncio.run(evaluate(args.languages, args.repeats, args.suite, args.only))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"Live grading: {report['passed']}/{report['total']} matched; model={report['model']}")
    return int(report['passed'] != report['total'])


if __name__ == '__main__':
    raise SystemExit(main())
