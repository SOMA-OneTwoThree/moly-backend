"""Partial publication cannot consume the single preparation restart."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import chat, jobs
from worker import consumer


@pytest.mark.parametrize('steps', [[100, 200, 400], [200, 300, 400]])
async def test_only_fixed_required_coverage_finishes_wait(monkeypatch, steps):
    session = SimpleNamespace(expunge_all=lambda: None, commit=AsyncMock(), rollback=AsyncMock())
    state = SimpleNamespace(deadline=time.monotonic()+2, model_reserve=0, generation=0,
                            baseline_through=100, required_through=400)
    validate = AsyncMock()
    monkeypatch.setattr(chat, '_validate_pressure_resume', validate)
    monkeypatch.setattr(chat.checkpoint_repo, 'load_latest', AsyncMock(side_effect=steps))
    async def coverage(*args, value, **kwargs):
        return SimpleNamespace(through_message_id=value)
    monkeypatch.setattr(chat.checkpoint_repo, 'coverage_for_checkpoint', coverage)
    monkeypatch.setattr(chat.asyncio, 'sleep', AsyncMock())
    assert await chat._wait_for_context_progress(session, 'synthetic-user', state)
    assert session.commit.await_count == len(steps)
    assert state.required_through == 400


async def test_partial_coverage_keeps_waiting_until_deadline(monkeypatch):
    session = SimpleNamespace(expunge_all=lambda: None, commit=AsyncMock(), rollback=AsyncMock())
    state = SimpleNamespace(deadline=time.monotonic()+0.03, model_reserve=0, generation=0,
                            baseline_through=100, required_through=400)
    monkeypatch.setattr(chat, '_validate_pressure_resume', AsyncMock())
    monkeypatch.setattr(chat.checkpoint_repo, 'load_latest', AsyncMock(return_value=200))
    monkeypatch.setattr(chat.checkpoint_repo, 'coverage_for_checkpoint',
                        AsyncMock(return_value=SimpleNamespace(through_message_id=200)))
    assert not await chat._wait_for_context_progress(session, 'synthetic-user', state)


@pytest.mark.parametrize('queue', [jobs.QUEUE_MEMORY, jobs.QUEUE_CONTENT, jobs.QUEUE_CRITICAL])
@pytest.mark.parametrize('base_delay', [1.0, 3.0])
async def test_only_empty_context_queues_have_one_second_cap(monkeypatch, queue, base_delay):
    stop = asyncio.Event()
    delays = []
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    async def sleep(stop, wake, seconds):
        delays.append(seconds)
        if len(delays) == 4:
            stop.set()
        return False
    monkeypatch.setattr(consumer, 'get_sessionmaker', lambda: Session)
    monkeypatch.setattr(jobs, 'claim', AsyncMock(return_value=[]))
    monkeypatch.setattr(consumer, '_sleep_stop_or_wake', sleep)
    monkeypatch.setattr(consumer.settings, 'job_idle_sleep_s', base_delay)
    monkeypatch.setattr(consumer.settings, 'job_idle_sleep_max_s', 8)
    await consumer.queue_loop(queue, 'synthetic-worker', stop)
    assert delays == ([1.0]*4 if queue != jobs.QUEUE_CRITICAL else
                      [base_delay, min(base_delay*2,8), min(base_delay*4,8),8])
