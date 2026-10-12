"""모니터링 — Slack severity 라우팅·dedup, 워커 결과게이트 데드맨·비용경보, config_store 세터."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import config_store, slack_notify
from worker import tick

_NOW = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Slack severity 라우팅 + dedup
# ---------------------------------------------------------------------------
class _FakeClient:
    def __init__(self):
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return SimpleNamespace(status_code=200, text="ok")


def _patch_client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(slack_notify.httpx, "AsyncClient", lambda **kw: fake)
    return fake


async def test_alert_routes_to_alert_webhook(monkeypatch):
    monkeypatch.setattr(slack_notify.settings, "slack_alert_webhook_url", "ALERT")
    monkeypatch.setattr(slack_notify.settings, "slack_status_webhook_url", "STATUS")
    fake = _patch_client(monkeypatch)
    await slack_notify.alert("경보")
    assert [c["url"] for c in fake.calls] == ["ALERT"]


async def test_status_routes_to_status_webhook(monkeypatch):
    monkeypatch.setattr(slack_notify.settings, "slack_alert_webhook_url", "ALERT")
    monkeypatch.setattr(slack_notify.settings, "slack_status_webhook_url", "STATUS")
    fake = _patch_client(monkeypatch)
    await slack_notify.send("상태", severity="status")
    assert [c["url"] for c in fake.calls] == ["STATUS"]


async def test_feedback_routes_to_feedback_webhook(monkeypatch):
    monkeypatch.setattr(slack_notify.settings, "slack_feedback_webhook_url", "FEEDBACK")
    monkeypatch.setattr(slack_notify.settings, "slack_alert_webhook_url", "ALERT")
    fake = _patch_client(monkeypatch)
    await slack_notify.send_feedback("피드백")
    assert [c["url"] for c in fake.calls] == ["FEEDBACK"]


async def test_feedback_falls_back_to_alert_webhook(monkeypatch):
    monkeypatch.setattr(slack_notify.settings, "slack_feedback_webhook_url", "")
    monkeypatch.setattr(slack_notify.settings, "slack_alert_webhook_url", "ALERT")
    monkeypatch.setattr(slack_notify.settings, "slack_webhook_url", "COMMON")
    fake = _patch_client(monkeypatch)
    await slack_notify.send_feedback("피드백")
    assert [c["url"] for c in fake.calls] == ["ALERT"]


async def test_feedback_falls_back_to_common_when_dedicated_and_alert_are_empty(monkeypatch):
    monkeypatch.setattr(slack_notify.settings, "slack_feedback_webhook_url", "")
    monkeypatch.setattr(slack_notify.settings, "slack_alert_webhook_url", "")
    monkeypatch.setattr(slack_notify.settings, "slack_webhook_url", "COMMON")
    fake = _patch_client(monkeypatch)
    await slack_notify.send_feedback("피드백")
    assert [c["url"] for c in fake.calls] == ["COMMON"]


async def test_falls_back_to_common_webhook(monkeypatch):
    monkeypatch.setattr(slack_notify.settings, "slack_alert_webhook_url", "")
    monkeypatch.setattr(slack_notify.settings, "slack_webhook_url", "COMMON")
    fake = _patch_client(monkeypatch)
    await slack_notify.alert("경보")
    assert [c["url"] for c in fake.calls] == ["COMMON"]


async def test_dedup_suppresses_within_window(monkeypatch):
    monkeypatch.setattr(slack_notify.settings, "slack_alert_webhook_url", "ALERT")
    monkeypatch.setattr(slack_notify.settings, "alert_dedup_window_sec", 300)
    slack_notify._last_sent.clear()
    fake = _patch_client(monkeypatch)
    await slack_notify.alert("x", dedup_key="k")
    await slack_notify.alert("x", dedup_key="k")  # 창 내 → 억제
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# 워커 결과게이트 데드맨 + 경보
# ---------------------------------------------------------------------------
class _GetClient:
    urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url):
        _GetClient.urls.append(url)


async def test_deadman_ping_ok_and_fail(monkeypatch):
    monkeypatch.setattr(tick.settings, "worker_ping_url", "https://hc.example/PING")
    monkeypatch.setattr(tick.httpx, "AsyncClient", lambda **kw: _GetClient())
    emit = AsyncMock()
    monkeypatch.setattr(tick.monitoring, "emit", emit)
    _GetClient.urls.clear()
    await tick._emit_worker_health(_NOW, {"diary_failed": 0, "memory_failed": 0})
    await tick._emit_worker_health(_NOW, {"diary_failed": 1, "memory_failed": 0})
    assert _GetClient.urls == ["https://hc.example/PING", "https://hc.example/PING/fail"]
    assert emit.await_count == 2


# ---------------------------------------------------------------------------
# config_store 세터 (upsert)
# ---------------------------------------------------------------------------
async def test_set_config_value_executes_and_commits():
    session = AsyncMock()
    await config_store.set_config_value(session, "monitoring:worker_last_success", "2026-07-20T04:00:00+00:00")
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()
