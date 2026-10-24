"""Explicit simulator QA server: dev DB/real AI, isolated local test identity.

uv run python -m scripts.serve_training_dev_test --output /absolute/evidence/directory
Run the mobile training_live_test.dart with output/device-config.json as
--dart-define-from-file. Stop with Ctrl-C after collecting device evidence.
No authentication provider settings or deployed servers are changed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid

import asyncpg
import httpx

from db.envfile import assert_dev_target, load_conn


def create_test_app():
    from fastapi import Header, HTTPException
    from app.core.security import get_current_user
    from app.main import create_app

    # A short-lived, loopback-only test identity; all domain services stay real.
    uid = os.environ['TRAINING_TEST_UID']
    token = os.environ['TRAINING_TEST_TOKEN']
    app = create_app()

    async def test_identity(authorization: str = Header()):
        if not secrets.compare_digest(authorization, f'Bearer {token}'):
            raise HTTPException(401)
        return uid

    app.dependency_overrides[get_current_user] = test_identity
    return app


def dev_parameter(name: str) -> str:
    result = subprocess.run(
        ['aws', '--profile', 'moly-prod', '--region', 'ap-northeast-2',
         'ssm', 'get-parameter', '--name', f'/moly/dev/{name}',
         '--with-decryption', '--output', 'json'],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)['Parameter']['Value']


async def serve(output: Path):
    dsn = load_conn('dev')
    assert_dev_target('dev', dsn)
    url = dev_parameter('supabase-url').rstrip('/')
    if url != 'https://wywzjslvxwttxkecbyis.supabase.co':
        raise ValueError('Unexpected dev Auth target')
    secret = dev_parameter('supabase-secret-key')
    key = dev_parameter('openai-api-key')
    output.mkdir(parents=True, exist_ok=True)
    config = output / 'device-config.json'
    if config.exists():
        raise ValueError('Existing device config: stop its server before replacing it')
    admin = {'apikey': secret, 'Authorization': f'Bearer {secret}'}
    async with httpx.AsyncClient(timeout=30) as http:
        created = await http.post(url + '/auth/v1/admin/users', headers=admin, json={
            'email': f'training-qa-{uuid.uuid4().hex}@example.com', 'email_confirm': True,
        })
        created.raise_for_status()
        uid = created.json()['id']
        proc = None
        try:
            from app.services.training_catalog import TrainingCatalog
            cases = {}
            evaluation = json.loads((Path(__file__).parents[1] / 'content/trainings/grading-eval.json').read_text())
            replies = {(case['course'], case['stage']): case['answers']['ko']
                       for case in evaluation if case['kind'] == 'natural_short' and 'ko' in case['answers']}
            for course in TrainingCatalog.load().trainings:
                if course.archived:
                    continue
                cases[course.id] = {'title': course.title.ko, 'stages': [
                    {'answer': (stage.grading.example.ko if stage.grading.example else replies[(course.id, stage.number)]) if stage.answer_type == 'write'
                     else next(o.id for o in stage.options if o.correct),
                     'wrong': next((o.id for o in stage.options if not o.correct), None)}
                    for stage in course.stages
                ]}
            token = secrets.token_urlsafe(32)
            with config.open('x') as handle:
                config.chmod(0o600)
                json.dump({'TRAINING_TEST_TOKEN': token,
                           'TRAINING_TEST_BASE_URL': 'http://127.0.0.1:18765',
                           'TRAINING_TEST_ANSWERS': json.dumps(cases, ensure_ascii=False)}, handle)
            (output / 'test-user-id.txt').write_text(uid)
            env = os.environ.copy()
            env.update(ENVIRONMENT='local', MOLY_ENV_FILE='/dev/null',
                       SUPABASE_URL=url, SUPABASE_DB_CONNECTION_STRING=dsn,
                       OPENAI_API_KEY=key, TRAINING_TEST_UID=uid, TRAINING_TEST_TOKEN=token)
            with (output / 'backend.log').open('w') as log:
                proc = subprocess.Popen(
                    [sys.executable, '-m', 'uvicorn',
                     'scripts.serve_training_dev_test:create_test_app', '--factory',
                     '--host', '127.0.0.1', '--port', '18765'],
                    env=env, stdout=log, stderr=log,
                )
                print('Dev test server starting on loopback port 18765.', flush=True)
                while proc.poll() is None:
                    await asyncio.sleep(1)
                if proc.returncode:
                    raise RuntimeError('Test server failed; inspect backend.log')
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                await asyncio.to_thread(proc.wait, 20)
            config.unlink(missing_ok=True)
            deleted = await http.delete(url + '/auth/v1/admin/users/' + uid, headers=admin)
            deleted.raise_for_status()
            conn = await asyncpg.connect(dsn, statement_cache_size=0)
            try:
                counts = {table: await conn.fetchval(
                    f'SELECT count(*) FROM public.{table} WHERE user_id=$1', uuid.UUID(uid))
                    for table in ['training_progress', 'training_answers', 'training_submissions', 'training_rewards']}
                (output / 'cleanup-counts.json').write_text(json.dumps(counts))
                if any(counts.values()):
                    raise RuntimeError('Test user cascade cleanup incomplete')
            finally:
                await conn.close()
            print('Test user deleted; cascade checked; token file removed.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.output.resolve()))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
