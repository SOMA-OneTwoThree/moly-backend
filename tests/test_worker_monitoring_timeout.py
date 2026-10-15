"""운영 관측은 본 작업의 제한 시간을 넘기거나 취소를 삼키지 않는다."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from app.services import monitoring_state as state
from worker import monitoring, tick

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


@pytest.mark.parametrize("external_cancel", [False, True])
async def test_stalled_transport_is_cancelled_without_success_ack(monkeypatch, external_cancel):
    session = SimpleNamespace(commit=AsyncMock())
    @asynccontextmanager
    async def context():
        yield session
    monkeypatch.setattr(state, "get_sessionmaker", lambda: context)
    claim = state.Claim("synthetic-key", "lease", True, "a", "alert", False, "synthetic")
    monkeypatch.setattr(state, "_claim", AsyncMock(return_value=claim))
    ack = AsyncMock()
    monkeypatch.setattr(state, "_ack", ack)
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def send(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(state.slack_notify, "send", send)
    monkeypatch.setattr(monitoring, "_MONITORING_BUDGET_S", 60 if external_cancel else 0.01)
    task = asyncio.create_task(monitoring.emit(NOW, {"diary_failed": 1}))
    await asyncio.wait_for(started.wait(), timeout=1)
    if external_cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if external_cancel else TimeoutError):
        await asyncio.wait_for(task, timeout=1)
    assert cancelled.is_set()
    session.commit.assert_awaited_once()  # claim was durable before the stalled network call
    ack.assert_not_awaited()  # lease remains unacknowledged; expiry permits a later retry


async def test_tick_keeps_committed_work_and_maintenance_when_monitoring_times_out(monkeypatch, caplog):
    from tests.test_worker_tick import _fake_get_sessionmaker
    from worker import retention_jobs, privacy_sweep_jobs
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_get_sessionmaker([]))
    monkeypatch.setattr(tick, "_relevant_timezones", AsyncMock(return_value=set()))
    monkeypatch.setattr(tick, "_drain_rc_inbox", AsyncMock(return_value={"rc_processed": 2}))
    monkeypatch.setattr(tick.config_store, "set_config_value", AsyncMock())
    monkeypatch.setattr(tick.settings, "worker_ping_url", "")
    monkeypatch.setattr(tick.settings, "memory_sweep_enabled", True)
    retention = AsyncMock(return_value=1)
    privacy = AsyncMock()
    memory = AsyncMock()
    monkeypatch.setattr(retention_jobs, "enqueue_daily", retention)
    monkeypatch.setattr(privacy_sweep_jobs, "enqueue_sweep", privacy)
    monkeypatch.setattr(tick.memory_pipeline, "enqueue_memory_sweep", memory)
    stopped = asyncio.Event()
    async def stalled(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(monitoring, "_emit", stalled)
    monkeypatch.setattr(monitoring, "_MONITORING_BUDGET_S", 0.01)
    result = await asyncio.wait_for(tick.run_tick(NOW), timeout=1)
    assert result["rc_processed"] == 2 and result["retention_enqueued"] == 1
    for operation in (retention, privacy, memory):
        operation.assert_awaited_once()
    assert tick.config_store.WORKER_LAST_SUCCESS_KEY in [
        call.args[1] for call in tick.config_store.set_config_value.await_args_list
    ]
    assert stopped.is_set()
    assert "워커 헬스 emit 실패(무시): TimeoutError" in caplog.text
