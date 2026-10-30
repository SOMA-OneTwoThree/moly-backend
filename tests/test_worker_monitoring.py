from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import monitoring_state, slack_notify
from worker import monitoring

NOW = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)


@pytest.fixture
def observe(monkeypatch):
    @asynccontextmanager
    async def session():
        yield SimpleNamespace()
    monkeypatch.setattr(monitoring, "get_sessionmaker", lambda: session)
    monkeypatch.setattr(monitoring.daily_report, "record_tick", AsyncMock())
    rc = {env: {"failed": 0, "pending": 0, "fingerprint": "empty", "oldest": None}
          for env in ("PRODUCTION", "SANDBOX", "UNKNOWN")}
    monkeypatch.setattr(monitoring, "rc_snapshot", AsyncMock(return_value=rc))
    publish = AsyncMock(return_value=False)
    monkeypatch.setattr(monitoring_state, "publish", publish)
    return rc, publish


async def test_no_actionable_alert_for_idle_or_billable_threshold(observe):
    _, publish = observe
    await monitoring.emit(NOW - timedelta(hours=1), {"billable_yesterday": 999_999_999})
    assert all(call.args[0].active is not True for call in publish.call_args_list)
    assert "cost_spike" not in [call.args[0].signal for call in publish.call_args_list]


async def test_failures_and_success_are_separate_signals(observe):
    _, publish = observe
    await monitoring.emit(NOW, {"diary_failed": 1, "memory_ok": 1})
    observations = {call.args[0].signal: call.args[0] for call in publish.call_args_list}
    assert observations["worker_diary"].active is True
    assert observations["worker_memory"].active is False
    assert observations["rc_processor"].active is None
    assert observations["daily_status"].fingerprint == "2026-10-12"
    # 무거운 일일 집계는 claim 성공 뒤에만 호출하도록 lazy renderer로 전달한다.
    assert publish.call_args.kwargs["render"] is not None


async def test_sandbox_is_visible_without_per_event_alarm(observe):
    rc, publish = observe
    rc["SANDBOX"].update(failed=2, fingerprint="synthetic-digest")
    await monitoring.emit(NOW, {})
    active = [c.args[0].signal for c in publish.call_args_list if c.args[0].active]
    assert active == ["daily_status"]
    assert "failed 2건" in monitoring.rc_line("SANDBOX", rc["SANDBOX"], NOW)


async def test_processor_outage_is_actionable_even_with_sandbox_only(observe):
    rc, publish = observe
    rc["SANDBOX"].update(failed=2)
    await monitoring.emit(NOW, {"rc_exception": 1})
    assert any(c.args[0].signal == "rc_processor" and c.args[0].active for c in publish.call_args_list)


async def test_persisted_provider_hold_is_actionable_without_exception_counter(observe):
    rc, publish = observe
    rc["SANDBOX"].update(provider_unavailable=1, provider_fingerprint="synthetic-provider")
    await monitoring.emit(NOW, {"rc_exception": 0, "rc_pending": 1})
    assert any(c.args[0].signal == "rc_provider" and c.args[0].active for c in publish.call_args_list)


async def test_worker_worsening_is_tiered_and_timeouts_are_failures(observe):
    _, publish = observe
    await monitoring.emit(NOW, {"diary_failed": 1000, "timed_out": 1, "users": 2})
    obs = {c.args[0].signal: c.args[0] for c in publish.call_args_list}
    assert obs["worker_diary"].importance == 3 and obs["worker_diary"].fingerprint == "3"
    assert obs["worker_timeout"].active is True
    assert monitoring.failure_tier(5) == monitoring.failure_tier(19) == 2


async def test_failed_rc_query_does_not_emit_recovery(observe, monkeypatch):
    _, publish = observe
    monkeypatch.setattr(monitoring, "rc_snapshot", AsyncMock(side_effect=RuntimeError("private")))
    await monitoring.emit(NOW, {})
    observations = {c.args[0].signal: c.args[0] for c in publish.call_args_list}
    assert observations["rc_observation"].active is True
    assert "rc_production" not in observations and "rc_unknown" not in observations


def test_cost_window_uses_kst_calendar_half_open_interval():
    assert monitoring.cost_window(NOW) == (
        datetime(2026, 10, 10, 15, tzinfo=timezone.utc), datetime(2026, 10, 11, 15, tzinfo=timezone.utc),
    )
    assert monitoring.cost_window(NOW - timedelta(hours=13)) == monitoring.cost_window(NOW)
    assert monitoring.cost_window(NOW - timedelta(hours=14))[1] == datetime(2026, 10, 10, 15, tzinfo=timezone.utc)


def test_partial_unknown_cost_and_experiments_are_explicit():
    row = dict(calls=5, priced=2, cost=123456, unknown=3, cache_estimated=1, experiments=1)
    message = monitoring.cost_line(row, NOW)
    assert "$0.1235" in message and "미확정 3건" in message
    assert "실험 1건 포함" in message and "미계측 경로 제외" in message
    row.update(priced=0, cost=None, unknown=5)
    assert "$0" not in monitoring.cost_line(row, NOW)
    row["calls"] = 0
    assert "계측 기록 0건" in monitoring.cost_line(row, NOW)


async def test_snapshot_reads_canonical_queue_stats_and_marks_unknown(observe, monkeypatch):
    rc, _ = observe
    monkeypatch.setattr(monitoring.config_store, "get_config_values", AsyncMock(return_value={}))
    queues = AsyncMock(return_value={"memory": {"ready": 2, "running": 1, "dead": 0}})
    monkeypatch.setattr(monitoring.jobs, "queue_stats", queues)
    message = await monitoring._daily_message(NOW, rc)
    queues.assert_awaited_once()
    assert "actionable" not in message and "RC PRODUCTION" not in message
    assert "정기 실행 완료 시각 확인 필요" in message and "어제 AI 사용 비용: 집계 확인 필요" in message
    assert "🟢" not in message


async def test_daily_snapshot_keeps_idle_failure_visible_and_uses_existing_age(observe, monkeypatch):
    rc, _ = observe
    values = {monitoring.config_store.WORKER_LAST_SUCCESS_KEY: NOW.isoformat(),
              monitoring_state.key_for("worker_diary"): {"active": True}}
    reader = AsyncMock(return_value=values)
    monkeypatch.setattr(monitoring.config_store, "get_config_values", reader)
    monkeypatch.setattr(monitoring.jobs, "queue_stats", AsyncMock(return_value={
        "memory": {"ready": 1, "running": 0, "dead": 1, "oldest_dead_age_s": 7200},
    }))
    message = await monitoring._daily_message(NOW, rc, {})
    assert "🟡 확인할 사항 있음" in message
    assert "일기 처리 확인 필요" in message and "완료되지 못한 비동기 작업 1건" in message
    assert "provider" not in message
    assert reader.await_count == 1


async def test_slack_transport_log_body_and_url_are_not_exposed(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, json):
            logging.getLogger("httpx").info("HTTP POST %s", url)
            logging.getLogger("httpcore.http11").debug("private-feedback-marker")
            return SimpleNamespace(status_code=500, text="private-feedback-marker")
    monkeypatch.setattr(slack_notify.httpx, "AsyncClient", lambda **kw: Client())
    assert not await slack_notify._post("https://fake.invalid/fake-token", "private-feedback-marker", plain=True)
    assert "fake-token" not in caplog.text and "private-feedback-marker" not in caplog.text
    assert "HTTP 500" in caplog.text


async def test_slack_failed_send_does_not_consume_local_dedup(monkeypatch):
    slack_notify._last_sent.clear()
    post = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(slack_notify, "_post", post)
    assert not await slack_notify.send("test", dedup_key="synthetic")
    assert await slack_notify.send("test", dedup_key="synthetic")
    assert post.await_count == 2


async def test_push_outage_requires_main_threshold_and_recovery_requires_acceptance(observe, monkeypatch):
    _, publish = observe
    monkeypatch.setattr(monitoring.settings, "worker_push_outage_min", 20)
    await monitoring.emit(NOW, {"push_sent": 0, "push_transient": 15, "push_setup_error": 10})
    failure = next(c.args[0] for c in publish.await_args_list if c.args[0].signal == "worker_push")
    assert failure.active is True and "푸시 실패 25(수락 0)" in failure.message
    publish.reset_mock()
    for counts, expected in [({}, None), ({"push_invalid_token": 500}, None),
        ({"push_sent": 0, "push_transient": 2}, None), ({"push_sent": 1, "push_transient": 20}, False)]:
        await monitoring.emit(NOW, counts)
        obs = next(c.args[0] for c in publish.await_args_list if c.args[0].signal == "worker_push")
        assert obs.active is expected
        publish.reset_mock()
