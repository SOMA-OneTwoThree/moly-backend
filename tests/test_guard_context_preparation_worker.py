"""Preparation must never claim notification or unrelated maintenance queues."""
from unittest.mock import AsyncMock

import pytest

from scripts import run_guard_context_preparation_worker as worker


def test_target_validation_blocks_unknown_and_unconfirmed_production():
    with pytest.raises(ValueError):
        worker.validate_target('postgresql://postgres.unknown:p@aws-0-ap-northeast-2.pooler.supabase.com:6543/postgres', 'unknown', True)
    with pytest.raises(ValueError, match='closed_maintenance'):
        worker.validate_target('postgresql://postgres.qkgjlgzsharnilxnkytd:p@aws-0-ap-northeast-2.pooler.supabase.com:6543/postgres',
            'qkgjlgzsharnilxnkytd', False)
    worker.validate_target('postgresql://postgres.wywzjslvxwttxkecbyis:p@aws-0-ap-northeast-2.pooler.supabase.com:6543/postgres',
        'wywzjslvxwttxkecbyis', False)


async def test_runner_only_uses_memory_and_content_and_provides_drain_event(monkeypatch):
    from worker import consumer as consumer_module

    consumer = AsyncMock()
    monkeypatch.setattr(consumer_module, 'run_consumer', consumer)
    await worker.run()
    assert consumer.await_args.kwargs['queues'] == ('memory', 'content')
    assert consumer.await_args.kwargs['global_cleanup'] is False
    assert not consumer.await_args.kwargs['stop'].is_set()


async def test_preparation_reaper_does_not_run_unrelated_global_cleanup(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from worker import consumer as module

    class Context:
        async def __aenter__(self):
            return object()
        async def __aexit__(self, *args):
            pass

    stop = asyncio.Event()
    reap, scrub, fortune, ads = (AsyncMock() for _ in range(4))
    async def stop_after_tick(*args):
        stop.set()
    monkeypatch.setattr(module, 'get_sessionmaker', lambda: Context)
    monkeypatch.setattr(module, 'jobs', SimpleNamespace(reap_queue=reap, scrub_expired_payloads=scrub))
    monkeypatch.setattr(module, 'fortune_ads', SimpleNamespace(cleanup_expired_sessions=fortune))
    monkeypatch.setattr(module, 'ads', SimpleNamespace(cleanup_expired_sessions=ads))
    monkeypatch.setattr(module, '_sleep_or_stop', stop_after_tick)
    await module.reaper_loop(stop, ('memory', 'content'), global_cleanup=False)
    assert [call.args[1] for call in reap.await_args_list] == ['memory', 'content']
    scrub.assert_not_awaited()
    fortune.assert_not_awaited()
    ads.assert_not_awaited()
