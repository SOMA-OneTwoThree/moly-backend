"""헬스·모니터링 엔드포인트 — liveness/ready/deep/synthetic + 인증 fail-closed."""
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api import health
from app.core.db import get_session
from app.main import app

client = TestClient(app)


class _OkSession:
    async def execute(self, *a, **k):
        return None


class _BadSession:
    async def execute(self, *a, **k):
        raise RuntimeError("db down")


class _DeepSession:
    """deep용 — SELECT 1은 무시, UserDailyStats 집계는 .one()으로 (billable, users) 반환."""
    async def execute(self, *a, **k):
        return SimpleNamespace(one=lambda: (0, 0))


def _override(session_obj):
    async def _gen():
        yield session_obj
    return _gen


# --- /health (liveness) ---
def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["app"] == "moly-backend"


def test_health_exposes_version(monkeypatch):
    monkeypatch.setattr(health.settings, "git_sha", "abc1234")
    assert client.get("/health").json()["version"] == "abc1234"


# --- /health/ready (readiness, 공개) ---
def test_ready_ok():
    app.dependency_overrides[get_session] = _override(_OkSession())
    try:
        r = client.get("/health/ready")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and r.json()["db"] == "ok"


def test_ready_down_returns_503():
    app.dependency_overrides[get_session] = _override(_BadSession())
    try:
        r = client.get("/health/ready")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["db"] == "down"


# --- deep/synthetic 인증(fail-closed) ---
def test_deep_forbidden_when_token_unset_in_prod(monkeypatch):
    monkeypatch.setattr(health.settings, "environment", "production")
    monkeypatch.setattr(health.settings, "health_token", "")
    app.dependency_overrides[get_session] = _override(_OkSession())
    try:
        r = client.get("/health/deep")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_deep_unauthorized_on_wrong_token(monkeypatch):
    monkeypatch.setattr(health.settings, "environment", "production")
    monkeypatch.setattr(health.settings, "health_token", "secret")
    app.dependency_overrides[get_session] = _override(_OkSession())
    try:
        r = client.get("/health/deep", headers={"X-Health-Token": "wrong"})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401


def test_deep_ok_in_local_with_fresh_worker(monkeypatch):
    """local + 토큰 미설정 → 인증 통과(개발 편의). 워커 최근 성공 → 200·no-store."""
    monkeypatch.setattr(health.settings, "environment", "local")
    monkeypatch.setattr(health.settings, "health_token", "")

    async def _cfg(session, keys):
        return {"monitoring:worker_last_success": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr(health.config_store, "get_config_values", _cfg)
    app.dependency_overrides[get_session] = _override(_DeepSession())
    try:
        r = client.get("/health/deep")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["worker"]["stale"] is False
    assert r.headers["Cache-Control"] == "no-store"


def test_deep_degraded_when_worker_stale(monkeypatch):
    """워커 last_success 기록 없음 → stale → 503(degraded)."""
    monkeypatch.setattr(health.settings, "environment", "local")
    monkeypatch.setattr(health.settings, "health_token", "")

    async def _cfg(session, keys):
        return {}

    monkeypatch.setattr(health.config_store, "get_config_values", _cfg)
    app.dependency_overrides[get_session] = _override(_DeepSession())
    try:
        r = client.get("/health/deep")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["worker"]["stale"] is True


# --- /health/synthetic (의존성 능동점검) ---
def test_synthetic_ok_with_llm_mocked(monkeypatch):
    monkeypatch.setattr(health.settings, "synthetic_check_llm", True)

    async def _fake_generate(*a, **k):
        return SimpleNamespace(text="ok")

    monkeypatch.setattr(health.llm, "generate", _fake_generate)
    app.dependency_overrides[get_session] = _override(_OkSession())
    try:
        r = client.get("/health/synthetic")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["llm"]["status"] == "ok"


def test_synthetic_down_when_llm_raises(monkeypatch):
    monkeypatch.setattr(health.settings, "synthetic_check_llm", True)

    async def _boom(*a, **k):
        raise RuntimeError("llm api down")

    monkeypatch.setattr(health.llm, "generate", _boom)
    app.dependency_overrides[get_session] = _override(_OkSession())
    try:
        r = client.get("/health/synthetic")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["llm"]["status"] == "down"


def test_synthetic_skips_llm_when_disabled(monkeypatch):
    monkeypatch.setattr(health.settings, "synthetic_check_llm", False)
    app.dependency_overrides[get_session] = _override(_OkSession())
    try:
        r = client.get("/health/synthetic")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and r.json()["llm"]["status"] == "skipped"


def test_synthetic_down_when_db_raises(monkeypatch):
    """DB 도달 실패(LLM 아님) → 503."""
    monkeypatch.setattr(health.settings, "synthetic_check_llm", False)
    app.dependency_overrides[get_session] = _override(_BadSession())
    try:
        r = client.get("/health/synthetic")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["db"]["status"] == "down"
