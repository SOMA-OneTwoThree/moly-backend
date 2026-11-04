"""Live preparation keeps old requests, new requests and private jobs isolated."""
import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import jobs, retention_release
from app.services.retention_preparation_context import (
    Binding, binding_captured, current_binding, from_payload, use_binding,
)
from app.services.retention_release_read import capture_controls


async def test_old_request_stays_explicitly_unbound_across_global_activation(monkeypatch):
    initial = {'context_retention_active_preparation': None, 'crisis_guard_enabled': False}
    result = SimpleNamespace(all=lambda: list(initial.items()))
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    read = AsyncMock(side_effect=AssertionError('old request must not switch projection'))
    monkeypatch.setattr(retention_release, 'resolve', retention_release.resolve)
    from app.services import config_store
    monkeypatch.setattr(config_store, 'get_config_values', read)
    with use_binding(None):
        captured, value = await capture_controls(session, initial)
        assert value is None and captured['crisis_guard_enabled'] is False
        initial.update(context_retention_active_preparation=str(uuid.uuid4()), crisis_guard_enabled=True)
        assert await retention_release.resolve(session) is None
        assert binding_captured() and current_binding() is None
    assert 'FOR SHARE' in str(session.execute.await_args.args[0])
    read.assert_not_awaited()


async def test_new_request_pins_active_projection_even_when_writer_is_off(monkeypatch):
    identity = uuid.uuid4()
    values = {'context_retention_active_preparation': str(identity),
              'context_retention_v2_enabled': False}
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: list(values.items()))))
    check = AsyncMock()
    monkeypatch.setattr(retention_release, 'require_binding', check)
    with use_binding(None):
        captured, value = await capture_controls(session, values)
        assert value == Binding(identity, False)
        assert current_binding() == value
        assert captured['context_retention_v2_enabled'] is False
        check.assert_awaited_once_with(session, value, lock=True)


async def test_parallel_tasks_never_share_projection_identity():
    original = binding_captured(), current_binding()
    first, second = Binding(uuid.uuid4(), True), Binding(uuid.uuid4(), False)
    entered = asyncio.Event()
    async def one(value):
        with use_binding(value):
            entered.set()
            await asyncio.sleep(0)
            assert current_binding() == value
            return current_binding()
    assert await asyncio.gather(one(first), one(second)) == [first, second]
    assert (binding_captured(), current_binding()) == original


@pytest.mark.parametrize('payload', [
    {'retention_preparation_building': True},
    {'retention_preparation_id': 'not-a-uuid'},
    {'retention_preparation_id': str(uuid.uuid4()), 'retention_preparation_building': 'false'},
])
def test_invalid_job_projection_cannot_fall_back_to_public(payload):
    with pytest.raises(ValueError):
        from_payload(payload)


async def test_private_child_jobs_keep_scope_and_cannot_enqueue_public_ingestion():
    identity = uuid.uuid4()
    records = []
    async def execute(statement, params):
        records.append(params)
        return SimpleNamespace(first=lambda: [uuid.uuid4()])
    session = SimpleNamespace(execute=execute)
    with use_binding(Binding(identity, True)):
        assert await jobs.enqueue(session, queue='memory', job_type='mem0_ingest', dedup_key='fact') is None
        await jobs.enqueue(session, queue='content', job_type='conversation_checkpoint',
                           dedup_key='same-source', payload={'source': 'unchanged'})
        await jobs.enqueue(session, queue='memory', job_type='contract_compile', dedup_key='contract')
    assert len(records) == 2
    for item in records:
        body = json.loads(item['payload'])
        assert body['retention_preparation_id'] == str(identity)
        assert body['retention_preparation_building'] is True
        assert item['priority'] >= 500
        assert str(identity) in item['dedup_key']
    with use_binding(Binding(identity, False)):
        await jobs.enqueue(session, queue='content', job_type='conversation_checkpoint', dedup_key='same-source')
    assert records[-1]['dedup_key'] != records[0]['dedup_key']


async def test_completed_live_fact_hint_captures_current_projection(monkeypatch):
    from worker import consumer
    identity = uuid.uuid4()
    job = SimpleNamespace(payload={}, job_type='mem0_ingest')
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(consumer, 'get_sessionmaker', lambda: Session)
    monkeypatch.setattr(consumer, 'run_job', AsyncMock())
    monkeypatch.setattr(retention_release, 'resolve', AsyncMock(return_value=Binding(identity, False)))
    observed = []
    async def hook(_): observed.append(current_binding())
    monkeypatch.setattr(consumer, '_post_attempt', hook)
    await consumer._run_job_then_wake(job, object())
    assert observed == [Binding(identity, False)]
