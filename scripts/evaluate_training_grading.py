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


async def evaluate(languages=('ko',), repeats=1):
    cases = []
    catalog = TrainingCatalog.load()
    for course in catalog.trainings:
        if course.archived:
            continue
        for stage in course.stages:
            if stage.answer_type == 'write':
                for language in languages:
                    cases.append((course.id, stage, 'example', stage.grading.example.pick(language), True, language))
                    attacks = {'ko': '나는 뭘 해도 못하는 한심한 인간이야.',
                               'en': 'I am a worthless person who cannot do anything right.',
                               'ja': '私は何をしてもだめな、価値のない人間だ。'}
                    cases.append((course.id, stage, 'self_attack', attacks[language], False, language))
    for number, answer in [(3, '다음부터 시간 약속을 잘 지키겠습니다.'), (14, '제 잘못입니다. 죄송합니다.'), (20, '다음에는 시간을 잘 확인하겠습니다.')]:
        cases.append(('positive-circuit-v1', catalog.get('positive-circuit-v1').stage(number), 'off_mission_apology_or_plan', answer, False, 'ko'))
    for answer, expected in [('뭐래', False), ('ㅋㅋㅋ', False), ('밥이 마싯어서 기분조앗다', True),
                             ('기다리는동안 친구랑 얘기해서 조앗다', True)]:
        cases.append(('positive-circuit-v1', catalog.get('positive-circuit-v1').stage(3),
                      'qa_reaction_or_typo', answer, expected, 'ko'))
    gate = asyncio.Semaphore(3)
    async def one(tid, stage, kind, answer, expected, language):
        row = {'course': tid, 'stage': stage.number, 'kind': kind, 'expected': expected, 'language': language}
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
            except Exception as exc:
                row.update(error=type(exc).__name__, ok=False)
        return row
    rows = await asyncio.gather(*(one(*case) for _ in range(repeats) for case in cases))
    return {'model': settings.model_training_grade, 'prompt_version': training_grading.PROMPT_VERSION,
            'total': len(rows), 'passed': sum(row['ok'] for row in rows), 'results': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--languages', nargs='+', choices=['ko', 'en', 'ja'], default=['ko'])
    parser.add_argument('--repeats', type=int, choices=range(1, 4), default=1)
    args = parser.parse_args()
    report = asyncio.run(evaluate(args.languages, args.repeats))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"Live grading: {report['passed']}/{report['total']} matched; model={report['model']}")
    return int(report['passed'] != report['total'])


if __name__ == '__main__':
    raise SystemExit(main())
