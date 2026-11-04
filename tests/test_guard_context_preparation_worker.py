"""Preparation must never claim notification or unrelated maintenance queues."""
from unittest.mock import AsyncMock

import pytest

from scripts import run_guard_context_preparation_worker as worker


def test_target_validation_blocks_unknown_and_unacknowledged_production():
    with pytest.raises(ValueError):
        worker.validate_target('postgresql://postgres.unknown:p@host/postgres', 'unknown', True)
    with pytest.raises(ValueError):
        worker.validate_target('postgresql://postgres.qkgjlgzsharnilxnkytd:p@aws-0-ap-northeast-2.pooler.supabase.com:6543/postgres',
            'qkgjlgzsharnilxnkytd', False)
    worker.validate_target('postgresql://postgres.qkgjlgzsharnilxnkytd:p@aws-0-ap-northeast-2.pooler.supabase.com:6543/postgres',
        'qkgjlgzsharnilxnkytd', True)


async def test_runner_only_claims_exact_private_stage_and_keeps_public_workers_online(monkeypatch):
    import uuid
    from app.config import settings
    from app.core import db
    from app.services import retention_release_promotion as promotion
    from worker import consumer as consumer_module

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    identity, code = uuid.uuid4(), 'a'*40
    monkeypatch.setattr(settings, 'git_sha', code)
    monkeypatch.setattr(db, 'get_sessionmaker', lambda: Session)
    monkeypatch.setattr(promotion, 'registry', AsyncMock(return_value={'state':'building','code_sha':code}))
    consumer = AsyncMock()
    monkeypatch.setattr(consumer_module, 'run_consumer', consumer)
    await worker.run(identity, expected_code_sha=code)
    args = consumer.await_args.kwargs
    assert args['queues'] == ('memory', 'content')
    assert args['preparation_id'] == identity
    assert args['concurrency_cap'] == 1
    assert args['reap'] is False and args['global_cleanup'] is False
    assert not args['stop'].is_set()


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
