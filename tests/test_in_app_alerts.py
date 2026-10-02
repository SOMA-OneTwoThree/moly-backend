"""API 안 경보(미처리 예외·회상 타임아웃·느린 턴)와 워커 합성 점검 — 발동·미발동·억제·실패 격리."""
import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core import errors
from app.core.errors import register_error_handlers
from app.services import alerts, chat, slack_notify
from worker import synthetic_probe


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    slack_notify._last_sent.clear()
    alerts._recall_timeouts.clear()
    monkeypatch.setattr(alerts.settings, "alert_dedup_window_sec", 300)
    monkeypatch.setattr(alerts.settings, "alert_recall_timeouts", 5)
    monkeypatch.setattr(alerts.settings, "alert_recall_timeout_window_s", 900)
    monkeypatch.setattr(alerts.settings, "alert_slow_turn_ms", 15_000)
    monkeypatch.setattr(alerts.settings, "alert_slow_turn_dedup_s", 900)
    yield
    slack_notify._last_sent.clear()
    alerts._recall_timeouts.clear()


@pytest.fixture
def sent(monkeypatch):
    calls: list[str] = []

    async def _alert(text, *, dedup_key=None):
        calls.append(text)

    monkeypatch.setattr(alerts.slack_notify, "alert", _alert)
    return calls


@pytest.fixture
def clock(monkeypatch):
    """억제·집계가 보는 monotonic만 테스트가 움직인다(이벤트 루프 시계는 그대로)."""
    t = SimpleNamespace(now=10_000.0)
    fake = SimpleNamespace(monotonic=lambda: t.now)
    monkeypatch.setattr(alerts, "time", fake)
    monkeypatch.setattr(slack_notify, "time", fake)
    return t


async def _drain():
    """띄운 경보 태스크를 끝까지 돌린다."""
    while alerts._pending:
        await asyncio.gather(*list(alerts._pending))


def _request(path="/v2/conversations/123e4567-e89b-12d3-a456-426614174000/messages", route=None):
    scope = {"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""}
    if route is not None:
        scope["route"] = SimpleNamespace(path=route)
    return Request(scope)


# ── 미처리 예외 ────────────────────────────────────────────────────────────────

async def test_unhandled_error_alerts_once_per_kind_with_route_template(sent):
    req = _request(route="/v2/conversations/{conversation_id}/messages")
    for _ in range(3):
        r = await errors._unhandled_exception_handler(req, RuntimeError("secret detail"))
        assert r.status_code == 500
    await errors._unhandled_exception_handler(req, KeyError("x"))
    await _drain()
    assert len(sent) == 2  # 같은 종류는 억제 창 안에서 1번
    assert "RuntimeError" in sent[0] and "POST /v2/conversations/{conversation_id}/messages" in sent[0]
    assert "secret detail" not in sent[0]  # 예외 메시지는 싣지 않는다
    assert "KeyError" in sent[1]


async def test_unhandled_error_hold_stays_5_minutes(sent, clock):
    req = _request(route="/x")
    t0 = clock.now
    for minutes in (0, 4, 6):
        clock.now = t0 + minutes * 60
        await errors._unhandled_exception_handler(req, RuntimeError("boom"))
    await _drain()
    assert len(sent) == 2  # 0분·6분 — 4분은 억제


async def test_unhandled_error_masks_ids_when_route_is_unknown(sent):
    await errors._unhandled_exception_handler(_request(), ValueError("boom"))
    await _drain()
    assert "POST /v2/conversations/{id}/messages" in sent[0]
    assert "123e4567" not in sent[0]


def test_500_body_unchanged_even_if_alerting_breaks(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("alerting broke")

    monkeypatch.setattr(errors.alerts, "fire", _boom)
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/crash")
    def crash():
        raise ValueError("boom")

    r = TestClient(app, raise_server_exceptions=False).get("/crash")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL"


async def test_alert_send_failure_stays_in_the_task(monkeypatch):
    async def _fail(text, *, dedup_key=None):
        raise RuntimeError("slack down")

    monkeypatch.setattr(alerts.slack_notify, "alert", _fail)
    alerts.fire("x", dedup_key="k")
    await _drain()  # 예외가 새지 않는다


def test_fire_without_running_loop_does_not_consume_dedup(sent):
    alerts.fire("x", dedup_key="sync")
    assert "sync" not in slack_notify._last_sent


# ── 회상 타임아웃 ──────────────────────────────────────────────────────────────

async def test_recall_timeouts_alert_at_threshold_then_count_restarts(sent, clock):
    for _ in range(4):
        alerts.recall_timeout()
        clock.now += 1
    await _drain()
    assert sent == []
    alerts.recall_timeout()
    await _drain()
    assert len(sent) == 1 and "회상 타임아웃 5회/15분" in sent[0]
    assert not alerts._recall_timeouts  # 경보 하나 = 새 타임아웃 5건 — 보내면 다시 0부터


async def test_recall_alert_holds_for_the_counting_window(sent, clock):
    for _ in range(5):
        alerts.recall_timeout()
    t0 = clock.now
    clock.now = t0 + 4 * 60
    for _ in range(5):
        alerts.recall_timeout()  # 새로 5건이어도 4분 뒤라 억제
    clock.now = t0 + 10 * 60
    alerts.recall_timeout()  # 10분 뒤에도 억제(5분 억제였다면 여기서 나간다)
    await _drain()
    assert len(sent) == 1 and len(alerts._recall_timeouts) == 6  # 억제된 건 비우지 않는다
    clock.now = t0 + 16 * 60
    alerts.recall_timeout()
    await _drain()
    assert len(sent) == 2 and "회상 타임아웃 7회/15분" in sent[1]
    assert not alerts._recall_timeouts


async def test_recall_timeouts_outside_window_do_not_count(sent, clock):
    for _ in range(4):
        alerts.recall_timeout()
    clock.now += 901
    alerts.recall_timeout()  # 앞의 넷은 15분 밖
    await _drain()
    assert sent == [] and len(alerts._recall_timeouts) == 1


async def test_recall_timeout_hook_in_chat(monkeypatch):
    seen = []
    monkeypatch.setattr(chat.alerts, "recall_timeout", lambda: seen.append(1))
    chat._log_recall_timeout(uuid.uuid4(), started=0.0, phase1_done=0.5, trace={})
    assert seen == [1]


# ── 느린 턴 ────────────────────────────────────────────────────────────────────

async def test_slow_turn_alerts_only_over_limit_with_breakdown(sent):
    alerts.slow_turn(15_000, phase1=900, llm=13_000, phase2=100)
    alerts.slow_turn(None)
    await _drain()
    assert sent == []
    alerts.slow_turn(16_250, phase1=900, llm=None, phase2=150)
    alerts.slow_turn(30_000)  # 억제 창 안
    await _drain()
    assert len(sent) == 1
    assert "16.2초" in sent[0] and "phase1 900ms" in sent[0] and "phase2 150ms" in sent[0]
    assert "llm" not in sent[0]


async def test_slow_turn_holds_for_15_minutes(sent, clock):
    alerts.slow_turn(16_000)
    t0 = clock.now
    for minutes in (4, 10):
        clock.now = t0 + minutes * 60
        alerts.slow_turn(16_000)  # 억제(5분 억제였다면 10분에 나간다)
    await _drain()
    assert len(sent) == 1
    clock.now = t0 + 16 * 60
    alerts.slow_turn(16_000)
    await _drain()
    assert len(sent) == 2


async def test_turn_metrics_feed_the_slow_turn_alert_except_replay(monkeypatch):
    seen = []
    monkeypatch.setattr(chat.alerts, "slow_turn", lambda total, **parts: seen.append((total, parts)))
    chat._emit_turn_metrics(replay=True, total_ms=40_000.0, phase1_ms=None, llm_ms=None, phase2_ms=None)
    assert seen == []  # replay는 원 턴이 따로 잡힌다
    chat._emit_turn_metrics(replay=False, total_ms=16_000.0, phase1_ms=1.0, llm_ms=2.0, phase2_ms=3.0)
    assert seen == [(16_000.0, {"phase1": 1.0, "llm": 2.0, "phase2": 3.0})]


async def test_disabled_thresholds_never_alert(monkeypatch, sent):
    monkeypatch.setattr(alerts.settings, "alert_recall_timeouts", 0)
    monkeypatch.setattr(alerts.settings, "alert_slow_turn_ms", 0)
    for _ in range(10):
        alerts.recall_timeout()
    alerts.slow_turn(99_000)
    await _drain()
    assert sent == [] and not alerts._recall_timeouts


# ── 워커 합성 점검 ──────────────────────────────────────────────────────────────

class _ProbeClient:
    """응답 순서대로 돌려주는 가짜 클라이언트. 요소가 예외면 던진다."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _resp(status, body=None):
    return httpx.Response(status, json=body if body is not None else {})


@pytest.fixture
def probe_env(monkeypatch):
    monkeypatch.setattr(synthetic_probe.settings, "environment", "production")
    monkeypatch.setattr(synthetic_probe.settings, "health_token", "test-token")
    monkeypatch.setattr(synthetic_probe.settings, "worker_synthetic_enabled", True)
    monkeypatch.setattr(synthetic_probe.settings, "worker_synthetic_url", "")
    monkeypatch.setattr(synthetic_probe.settings, "worker_synthetic_timeout_s", 20.0)
    monkeypatch.setattr(synthetic_probe, "_RETRY_DELAY_S", 0.0)
    alerts_sent: list[str] = []

    async def _alert(text, *, dedup_key=None):
        alerts_sent.append(text)

    monkeypatch.setattr(synthetic_probe.slack_notify, "alert", _alert)

    def _use(replies):
        client = _ProbeClient(replies)
        monkeypatch.setattr(synthetic_probe.httpx, "AsyncClient", lambda **kw: client)
        return client

    return SimpleNamespace(alerts=alerts_sent, use=_use)


@pytest.mark.parametrize(("env", "override", "enabled", "expected"), [
    ("production", "", True, synthetic_probe.PRODUCTION_SYNTHETIC_URL),
    ("development", "", True, ""),
    ("local", "", True, ""),
    ("development", "https://dev.example/health/synthetic", True, "https://dev.example/health/synthetic"),
    ("production", "", False, ""),
])
def test_target_url_defaults_to_production_only(monkeypatch, env, override, enabled, expected):
    monkeypatch.setattr(synthetic_probe.settings, "environment", env)
    monkeypatch.setattr(synthetic_probe.settings, "worker_synthetic_url", override)
    monkeypatch.setattr(synthetic_probe.settings, "worker_synthetic_enabled", enabled)
    assert synthetic_probe.target_url() == expected


async def test_no_probe_without_token(probe_env, monkeypatch):
    monkeypatch.setattr(synthetic_probe.settings, "health_token", "")
    assert synthetic_probe.start() is None
    await synthetic_probe.finish(None)
    assert probe_env.alerts == []


async def test_probe_ok_sends_token_header_and_no_alert(probe_env):
    client = probe_env.use([_resp(200, {"llm": {"latency_ms": 812}})])
    await synthetic_probe.finish(synthetic_probe.start())
    assert len(client.calls) == 1
    url, headers = client.calls[0]
    assert url == synthetic_probe.PRODUCTION_SYNTHETIC_URL and headers == {"X-Health-Token": "test-token"}
    assert probe_env.alerts == []


async def test_one_failure_then_ok_is_quiet(probe_env):
    client = probe_env.use([_resp(503), _resp(200)])
    await synthetic_probe.finish(synthetic_probe.start())
    assert len(client.calls) == 2 and probe_env.alerts == []


@pytest.mark.parametrize(("replies", "reason"), [
    ([_resp(503), _resp(503)], "HTTP 503"),
    ([httpx.ConnectError("x"), httpx.ConnectTimeout("y")], "ConnectTimeout"),
])
async def test_two_failures_alert_without_secrets(probe_env, replies, reason):
    probe_env.use(replies)
    await synthetic_probe.finish(synthetic_probe.start())
    assert len(probe_env.alerts) == 1 and reason in probe_env.alerts[0]
    assert "재시도도 실패" in probe_env.alerts[0]
    assert "test-token" not in probe_env.alerts[0]


async def test_hanging_probe_is_capped(probe_env, monkeypatch):
    async def _hang(url):
        await asyncio.sleep(60)

    monkeypatch.setattr(synthetic_probe, "_probe", _hang)
    monkeypatch.setattr(synthetic_probe.settings, "worker_synthetic_timeout_s", 0.01)
    monkeypatch.setattr(synthetic_probe, "_CAP_SLACK_S", 0.03)
    task = synthetic_probe.start()
    await synthetic_probe.finish(task)
    assert task.cancelled()
    assert len(probe_env.alerts) == 1
    assert "초 안에 응답 없음" in probe_env.alerts[0] and "재시도" not in probe_env.alerts[0]


async def test_cap_text_names_the_default_50_seconds(probe_env, monkeypatch):
    async def _timeout(url):
        raise TimeoutError  # wait_for가 상한에서 던지는 것과 같은 예외

    monkeypatch.setattr(synthetic_probe, "_probe", _timeout)
    monkeypatch.setattr(synthetic_probe, "_RETRY_DELAY_S", 5.0)  # 기본값(20초 × 2 + 5 + 5)
    await synthetic_probe.finish(synthetic_probe.start())
    assert "/health/synthetic 50초 안에 응답 없음" in probe_env.alerts[0]


async def test_probe_code_error_is_not_reported_as_a_failed_retry(probe_env, monkeypatch):
    async def _broken(url):
        raise ValueError("bad config")

    monkeypatch.setattr(synthetic_probe, "_probe", _broken)
    await synthetic_probe.finish(synthetic_probe.start())
    assert len(probe_env.alerts) == 1
    assert "점검 자체 오류" in probe_env.alerts[0] and "ValueError" in probe_env.alerts[0]
    assert "재시도" not in probe_env.alerts[0] and "bad config" not in probe_env.alerts[0]


async def test_alert_failure_never_escapes_finish(probe_env, monkeypatch):
    async def _fail(text, *, dedup_key=None):
        raise RuntimeError("slack down")

    monkeypatch.setattr(synthetic_probe.slack_notify, "alert", _fail)
    probe_env.use([_resp(500), _resp(500)])
    await synthetic_probe.finish(synthetic_probe.start())  # 던지지 않는다


async def test_worker_entry_finishes_probe_even_when_tick_fails(monkeypatch):
    import worker.__main__ as entry

    order = []

    async def _tick(now):
        order.append("tick")
        raise RuntimeError("tick failed")

    async def _flush():
        order.append("flush")

    async def _finish(task):
        order.append(("finish", task))

    monkeypatch.setattr(entry, "run_tick", _tick)
    monkeypatch.setattr(entry.synthetic_probe, "start", lambda: "probe")
    monkeypatch.setattr(entry.synthetic_probe, "finish", _finish)
    from app.services import usage_ledger

    monkeypatch.setattr(usage_ledger, "flush_closes", _flush)
    with pytest.raises(RuntimeError):
        await entry._tick_then_flush()
    assert order == ["tick", "flush", ("finish", "probe")]
