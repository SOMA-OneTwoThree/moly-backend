"""헬스·모니터링 엔드포인트 — liveness/ready/deep/synthetic + 인증 fail-closed."""
import asyncio
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


# --- deep: retention 잡 stale 판정(app_config 기록 기반 — Phase 5 교차검증 [중-1]) ---
def _fresh_worker_cfg(extra: dict | None = None):
    """worker fresh + retention 키를 얹은 config_store 페이크."""
    base = {"monitoring:worker_last_success": datetime.now(timezone.utc).isoformat()}
    base.update(extra or {})

    async def _cfg(session, keys):
        return base

    return _cfg


def test_deep_retention_never_run_is_not_stale(monkeypatch):
    """일간 잡만 기록되고 월간 rc 잡이 아직 None이어도 503이 아니다 — async_jobs 이력
    기반이던 시절엔 이 상황(배포~다음달 1일, 그리고 14일 GC 이후 매달 후반)이 상시 503이었다."""
    monkeypatch.setattr(health.settings, "environment", "local")
    monkeypatch.setattr(health.settings, "health_token", "")
    fresh = datetime.now(timezone.utc).isoformat()
    daily = {f"monitoring:retention_last_success:{jt}": fresh
             for jt in ("retention_idempotency_gc", "usage_ledger_rollup",
                        "retention_jobs_gc", "mem0_candidate_gc")}  # rc_events 없음
    monkeypatch.setattr(health.config_store, "get_config_values", _fresh_worker_cfg(daily))
    app.dependency_overrides[get_session] = _override(_DeepSession())
    try:
        r = client.get("/health/deep")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    body = r.json()
    assert body["retention"]["stale"] is False
    assert body["retention"]["jobs"]["retention_rc_events"]["last_success"] is None


def test_deep_retention_stale_daily_degrades(monkeypatch):
    """일간 잡 기록이 25h를 넘으면 503 — 기록은 GC와 무관하게 영구 보존이라 판정이 견고하다."""
    monkeypatch.setattr(health.settings, "environment", "local")
    monkeypatch.setattr(health.settings, "health_token", "")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    monkeypatch.setattr(
        health.config_store, "get_config_values",
        _fresh_worker_cfg({"monitoring:retention_last_success:usage_ledger_rollup": old}),
    )
    app.dependency_overrides[get_session] = _override(_DeepSession())
    try:
        r = client.get("/health/deep")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["retention"]["stale"] is True


def test_deep_vecs_bytes_per_row_none_before_analyze(monkeypatch):
    """ANALYZE 전 reltuples=-1 — 음수 팽창비 대신 None(교차검증 [하-1])."""
    monkeypatch.setattr(health.settings, "environment", "local")
    monkeypatch.setattr(health.settings, "health_token", "")
    monkeypatch.setattr(health.config_store, "get_config_values", _fresh_worker_cfg())

    class _TablesSession(_DeepSession):
        async def execute(self, stmt, *a, **k):
            if "pg_total_relation_size" in str(stmt):
                return SimpleNamespace(one=lambda: (10, 10, 10, 1000, -1.0))
            return SimpleNamespace(one=lambda: (0, 0))

    app.dependency_overrides[get_session] = _override(_TablesSession())
    try:
        r = client.get("/health/deep")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json()["tables"]["vecs_bytes_per_row"] is None


# --- deep: 타임존 해석(2026-10-01 레거시 별칭 41명이 두 달간 어떤 지표에도 안 잡힘) ---
def _tz_session(names):
    """profiles.timezone distinct 조회만 scalars로, 나머지는 _DeepSession과 같게."""
    class _TzSession(_DeepSession):
        async def execute(self, stmt, *a, **k):
            if "profiles.timezone" in str(stmt):
                return SimpleNamespace(scalars=lambda: iter(names))
            return SimpleNamespace(one=lambda: (0, 0))

    return _TzSession()


def _get_deep(monkeypatch, names, extra_cfg=None):
    monkeypatch.setattr(health.settings, "environment", "local")
    monkeypatch.setattr(health.settings, "health_token", "")
    monkeypatch.setattr(health.config_store, "get_config_values", _fresh_worker_cfg(extra_cfg))
    app.dependency_overrides[get_session] = _override(_tz_session(names))
    try:
        return client.get("/health/deep")
    finally:
        app.dependency_overrides.clear()


def test_deep_timezones_ok_before_first_worker_record(monkeypatch):
    """전부 해석되고 워커 기록이 아직 없으면(배포 직후) 200 — 기록 없음은 None."""
    r = _get_deep(monkeypatch, ["Asia/Seoul", "Asia/Calcutta", None])
    assert r.status_code == 200
    assert r.json()["timezones"] == {"distinct": 2, "unresolvable_api": [], "unresolvable_worker": None}


def test_deep_degraded_when_profile_timezone_unresolvable(monkeypatch):
    r = _get_deep(monkeypatch, ["Asia/Seoul", "Not/AZone"],
                  {"monitoring:worker_tz_unresolvable": []})
    assert r.status_code == 503
    assert r.json()["timezones"]["unresolvable_api"] == ["Not/AZone"]


def test_deep_degraded_when_worker_recorded_unresolvable(monkeypatch):
    """API 컨테이너는 풀어도 워커 틱이 못 푼 tz가 있으면 503(두 프로세스의 tzdata가 어긋난 경우)."""
    r = _get_deep(monkeypatch, ["Asia/Seoul"], {"monitoring:worker_tz_unresolvable": ["Not/AZone"]})
    assert r.status_code == 503
    assert r.json()["timezones"]["unresolvable_worker"] == ["Not/AZone"]


# --- /health/queues (잡 큐 — 이관 게이트 지표) ---
def test_queues_exposes_counts_and_oldest_dead_age(monkeypatch):
    monkeypatch.setattr(health.settings, "environment", "local")
    monkeypatch.setattr(health.settings, "health_token", "")

    async def _stats(session):
        return {
            "critical": {"ready": 0, "running": 0, "dead": 0, "oldest_dead_age_s": None},
            "content": {"ready": 3, "running": 1, "dead": 2, "oldest_dead_age_s": 900},
        }

    monkeypatch.setattr(health.jobs, "queue_stats", _stats)
    app.dependency_overrides[get_session] = _override(_OkSession())
    try:
        r = client.get("/health/queues")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    body = r.json()
    assert body["queues"]["content"] == {
        "ready": 3, "running": 1, "dead": 2, "oldest_dead_age_s": 900
    }
    assert body["dead_total"] == 2  # 이관 게이트: dead 증가·미확인은 배포 gate 실패
    assert r.headers["Cache-Control"] == "no-store"


def test_queues_requires_token_in_prod(monkeypatch):
    monkeypatch.setattr(health.settings, "environment", "production")
    monkeypatch.setattr(health.settings, "health_token", "")
    app.dependency_overrides[get_session] = _override(_OkSession())
    try:
        r = client.get("/health/queues")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 403


def test_queues_down_when_db_unreachable(monkeypatch):
    monkeypatch.setattr(health.settings, "environment", "local")
    monkeypatch.setattr(health.settings, "health_token", "")

    async def _boom(session):
        raise RuntimeError("db down")

    monkeypatch.setattr(health.jobs, "queue_stats", _boom)
    app.dependency_overrides[get_session] = _override(_OkSession())
    try:
        r = client.get("/health/queues")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["status"] == "down"


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


def _synthetic_with(monkeypatch, fake_generate):
    monkeypatch.setattr(health.settings, "synthetic_check_llm", True)
    monkeypatch.setattr(health.llm, "generate", fake_generate)
    app.dependency_overrides[get_session] = _override(_OkSession())
    try:
        return client.get("/health/synthetic")
    finally:
        app.dependency_overrides.clear()


def test_synthetic_calls_the_llm_like_the_chat_path(monkeypatch):
    """대화 경로처럼 추론을 끄고 SDK 재시도 없이 상한 안에서 부른다.

    기본값(추론 켬·SDK 재시도)으로 부르면 시도마다 timeout이 새로 붙어 느려도 200이 된다.
    """
    seen: dict = {}

    async def _fake_generate(*a, **k):
        seen.update(k)
        return SimpleNamespace(text="pong")

    r = _synthetic_with(monkeypatch, _fake_generate)
    assert r.status_code == 200 and r.json()["llm"]["status"] == "ok"
    assert seen["reasoning_effort"] == "none" and seen["sdk_retries"] is False
    assert seen["timeout"] == health.settings.synthetic_llm_timeout_s
    assert isinstance(r.json()["llm"]["latency_ms"], int)


def test_synthetic_empty_text_is_still_up(monkeypatch):
    """빈 텍스트도 도달은 정상 — `empty`로만 알리고 503으로 바꾸지 않는다."""

    async def _empty(*a, **k):
        return SimpleNamespace(text="  ")

    r = _synthetic_with(monkeypatch, _empty)
    assert r.status_code == 200
    assert r.json()["llm"]["status"] == "ok" and r.json()["llm"]["empty"] is True


def test_synthetic_over_the_limit_is_down_with_latency(monkeypatch):
    """상한을 넘기면 down(503)이고, 실패에도 지연이 남는다."""
    monkeypatch.setattr(health.settings, "synthetic_llm_timeout_s", 0.05)

    async def _hang(*a, **k):
        await asyncio.sleep(5)
        return SimpleNamespace(text="late")

    r = _synthetic_with(monkeypatch, _hang)
    assert r.status_code == 503
    out = r.json()["llm"]
    assert out["status"] == "down" and out["error"] == "TimeoutError"
    assert 1000 <= out["latency_ms"] < 5000  # 상한(0.05s) + 바깥 여유 1s에서 끊김
