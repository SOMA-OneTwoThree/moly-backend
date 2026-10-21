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


async def evaluate():
    cases = []
    catalog = TrainingCatalog.load()
    for course in catalog.trainings:
        if course.archived:
            continue
        for stage in course.stages:
            if stage.answer_type == 'write':
                cases.append((course.id, stage, 'example', stage.grading.example.ko, True))
                cases.append((course.id, stage, 'self_attack', '나는 뭘 해도 못하는 한심한 인간이야.', False))
    for number, answer in [(3, '다음부터 시간 약속을 잘 지키겠습니다.'), (14, '제 잘못입니다. 죄송합니다.'), (20, '다음에는 시간을 잘 확인하겠습니다.')]:
        cases.append(('positive-circuit-v1', catalog.get('positive-circuit-v1').stage(number), 'off_mission_apology_or_plan', answer, False))
    gate = asyncio.Semaphore(3)
    async def one(tid, stage, kind, answer, expected):
        row = {'course': tid, 'stage': stage.number, 'kind': kind, 'expected': expected}
        async with gate:
            try:
                result = await llm.generate(
                    training_grading.system_prompt(stage, 'ko'),
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
    rows = await asyncio.gather(*(one(*case) for case in cases))
    return {'model': settings.model_training_grade, 'prompt_version': training_grading.PROMPT_VERSION,
            'total': len(rows), 'passed': sum(row['ok'] for row in rows), 'results': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"Live grading: {report['passed']}/{report['total']} matched; model={report['model']}")
    return int(report['passed'] != report['total'])


if __name__ == '__main__':
    raise SystemExit(main())
