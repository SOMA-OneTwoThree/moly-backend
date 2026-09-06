"""Bounded operator runner stops claiming, propagates failure and never defaults to prod."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.core import db
from db import envfile
from scripts import run_memory_consumer as runner
from worker import consumer


@pytest.mark.parametrize('scenario', ['bounded', 'until_empty_timeout', 'dead', 'consumer_error'])
async def test_consumer_runner_bounds_and_failure_exit(monkeypatch, scenario):
    argv = ['run_memory_consumer.py', '--env', 'dev', '--seconds', '0.001']
    if scenario == 'until_empty_timeout':
        argv.append('--until-empty')
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setenv('MOLY_ENV_FILE', '.env.prod')
    monkeypatch.setattr(envfile, 'configure_application_db', lambda env: 'postgresql://test@localhost/local')
    monkeypatch.setattr(envfile, 'announce', lambda *a, **k: None)
    monkeypatch.setattr(db, 'get_sessionmaker', lambda: object())
    calls = []
    async def pending(maker):
        return (1, 0, int(scenario == 'dead'))
    async def run(*, queues, stop):
        calls.append(queues)
        if scenario == 'consumer_error':
            raise RuntimeError('local injected error')
        await stop.wait()
    monkeypatch.setattr(runner, '_pending', pending)
    monkeypatch.setattr(consumer, 'run_consumer', run)
    if scenario == 'consumer_error':
        with pytest.raises(RuntimeError, match='local injected error'):
            await asyncio.wait_for(runner.main(), 1)
    else:
        assert await asyncio.wait_for(runner.main(), 1) == int(scenario != 'bounded')
    assert calls == ([] if scenario == 'dead' else [('memory',)])
    assert os.environ['MOLY_ENV_FILE'] == '.env'


@pytest.mark.parametrize('scenario', ['preview', 'apply', 'failure', 'missing_env'])
def test_batch_shell_is_bounded_and_propagates_failure(tmp_path, scenario):
    script = Path(__file__).resolve().parents[1] / 'scripts/backfill_memory_batches.sh'
    stub = tmp_path / 'uv'
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$MOLY_CALL_LOG"\nexit "${MOLY_TEST_FAIL:-0}"\n')
    stub.chmod(0o755)
    logfile = tmp_path / 'calls'
    env = dict(os.environ, PATH=str(tmp_path)+':'+os.environ['PATH'], MOLY_CALL_LOG=str(logfile))
    args = ['bash', str(script), '--batches', '2']
    if scenario != 'missing_env':
        args += ['--env', 'dev']
    if scenario in {'apply', 'failure'}:
        args.append('--yes')
    if scenario == 'failure':
        env['MOLY_TEST_FAIL'] = '7'
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=10)
    calls = logfile.read_text().splitlines() if logfile.exists() else []
    assert result.returncode == {'failure': 7, 'missing_env': 2}.get(scenario, 0), result.stderr
    assert len(calls) == {'preview': 1, 'apply': 4, 'failure': 1, 'missing_env': 0}[scenario]
    assert all('--env dev' in call for call in calls)
    if scenario == 'preview':
        assert '--yes' not in calls[0]
        assert 'run_memory_consumer' not in calls[0]
