"""탈퇴 잔여 sweep — 예약과 대상 조건. 실제 SQL 실행은 tests/integration/test_privacy_deletion_local.py."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.services import jobs
from worker import privacy_sweep_jobs as sweep
from worker import tick


async def test_sweep_is_a_maintenance_job_without_user(monkeypatch):
    """user_id가 있으면 계정 삭제 때 CASCADE로 같이 지워진다 — 이 잡은 반드시 user_id 없이 건다."""
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    await sweep.enqueue_sweep(object(), bucket="20261005T1900")
    kwargs = enqueue.await_args.kwargs
    assert kwargs["queue"] == jobs.QUEUE_MAINTENANCE
    assert kwargs["job_type"] == sweep.JOB_PRIVACY_RESIDUAL_SWEEP
    assert kwargs["dedup_key"] == "privsweep:20261005T1900"
    assert "user_id" not in kwargs


@pytest.mark.parametrize("sql", [sweep._PROMOTE_ORPHANS, sweep._SUBJECTS])
def test_sweep_touches_only_accounts_that_no_longer_exist(sql):
    """프로필만 없고 인증 계정이 남은 경우(자가 복구로 프로필이 다시 생김)도 대상이 아니다."""
    s = str(sql)
    assert "NOT EXISTS (SELECT 1 FROM profiles p WHERE p.id = b.user_id)" in s
    assert "NOT EXISTS (SELECT 1 FROM auth.users u WHERE u.id = b.user_id)" in s


@pytest.fixture
def quiet_tick(monkeypatch):
    from worker import retention_jobs
    from tests.test_worker_tick import _fake_get_sessionmaker

    monkeypatch.setattr(tick, "get_sessionmaker", _fake_get_sessionmaker([]))
    monkeypatch.setattr(tick, "_drain_rc_inbox", AsyncMock(return_value={}))
    monkeypatch.setattr(tick.slack_notify, "send_summary", AsyncMock())
    monkeypatch.setattr(tick.slack_notify, "alert", AsyncMock())
    monkeypatch.setattr(tick.config_store, "set_config_value", AsyncMock())
    monkeypatch.setattr(tick.memory_pipeline, "enqueue_memory_sweep", AsyncMock())
    monkeypatch.setattr(tick.settings, "worker_ping_url", "")
    monkeypatch.setattr(tick.settings, "daily_billable_alert_threshold", 0)
    monkeypatch.setattr(retention_jobs, "enqueue_daily", AsyncMock(return_value=0))


async def test_every_tick_books_one_sweep_for_its_window(monkeypatch, quiet_tick):
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(sweep, "enqueue_sweep", enqueue)
    await tick.run_tick(datetime(2026, 10, 5, 19, 15, tzinfo=timezone.utc))
    assert enqueue.await_args.kwargs == {"bucket": "20261005T1915"}


async def test_a_failed_sweep_booking_does_not_break_the_tick(monkeypatch, quiet_tick):
    monkeypatch.setattr(sweep, "enqueue_sweep", AsyncMock(side_effect=RuntimeError("db down")))
    health = AsyncMock()
    monkeypatch.setattr(tick, "_emit_worker_health", health)
    await tick.run_tick(datetime(2026, 10, 5, 19, 15, tzinfo=timezone.utc))
    health.assert_awaited_once()  # 예약 뒤 단계(데드맨 핑)까지 돌았다
