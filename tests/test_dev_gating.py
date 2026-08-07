"""SOMA-376: /dev 라우트는 명시 플래그(ENABLE_DEV_ROUTES)로만 등록 — 프로덕션 노출 방지(fail-closed).

라우터는 커스텀 _IncludedRouter로 등록돼 app.routes에 path가 안 뜨므로, TestClient로 판정한다
(미등록=404 / 등록=인증 전 단계에서 401 등 404 아님).
"""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.main import create_app


def _dev_registered(app) -> bool:
    return TestClient(app).post("/dev/diaries/generate").status_code != 404


def _production_ready(monkeypatch) -> None:
    """ambient .env와 무관하게 비-local 부팅 가드만 통과시킨다."""
    monkeypatch.setattr(settings, "revenuecat_webhook_auth", "x")
    monkeypatch.setattr(settings, "openai_api_key", "x")


def test_enable_dev_routes_defaults_false():
    assert Settings().enable_dev_routes is False


def test_swagger_is_exposed_on_isolated_development_server(monkeypatch):
    _production_ready(monkeypatch)
    monkeypatch.setattr(settings, "environment", "development")
    app = create_app()
    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"


@pytest.mark.parametrize("environment", ["staging", "production", "unexpected"])
def test_swagger_is_hidden_outside_local_and_development(monkeypatch, environment):
    _production_ready(monkeypatch)
    monkeypatch.setattr(settings, "environment", environment)
    app = create_app()
    assert app.docs_url is None
    assert app.openapi_url is None


def test_dev_router_not_registered_by_default(monkeypatch):
    # ENVIRONMENT 누락으로 local로 새어도 플래그 off면 /dev 미노출.
    monkeypatch.setattr(settings, "enable_dev_routes", False)
    monkeypatch.setattr(settings, "environment", "local")
    assert not _dev_registered(create_app())


def test_dev_router_registered_when_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(settings, "enable_dev_routes", True)
    monkeypatch.setattr(settings, "environment", "local")
    assert _dev_registered(create_app())


def test_dev_router_never_in_nonlocal_env(monkeypatch):
    # local이 아닌 임의 환경(staging·prod 등)에선 플래그가 켜져도 미등록(sol #4).
    monkeypatch.setattr(settings, "enable_dev_routes", True)
    monkeypatch.setattr(settings, "environment", "staging")
    # 비-local이라 require_production_ready가 시크릿을 강제 — CI(시크릿 없음)에서도 통과하도록 주입.
    _production_ready(monkeypatch)
    assert not _dev_registered(create_app())


def test_dev_router_never_in_production(monkeypatch):
    # 프로덕션에선 플래그가 켜져 있어도 /dev를 등록하지 않는다(이중 방어).
    monkeypatch.setattr(settings, "enable_dev_routes", True)
    monkeypatch.setattr(settings, "environment", "production")
    _production_ready(monkeypatch)
    assert not _dev_registered(create_app())
