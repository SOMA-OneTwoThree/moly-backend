"""GET /app-config 라우팅(의존성 오버라이드로 DB 없이) + 에러 응답 형식 검증."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import AppError, insufficient_hay, register_error_handlers
from app.main import app
from app.services.app_config import get_public_app_config


def test_app_config_returns_override():
    async def _fake_cfg():
        return {
            "min_supported_version": "1.2.0",
            "maintenance": {"active": False, "message": None},
            "day_night_schedule": None,
        }

    app.dependency_overrides[get_public_app_config] = _fake_cfg
    try:
        r = TestClient(app).get("/app-config")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json()["min_supported_version"] == "1.2.0"


def test_app_error_format():
    probe = FastAPI()
    register_error_handlers(probe)

    @probe.get("/boom")
    def boom():
        raise insufficient_hay(required=1000, balance=640)

    r = TestClient(probe).get("/boom")
    assert r.status_code == 402
    body = r.json()
    assert body["error"]["code"] == "INSUFFICIENT_HAY"
    assert body["error"]["details"] == {"required": 1000, "balance": 640}


def test_app_error_default_details_empty():
    probe = FastAPI()
    register_error_handlers(probe)

    @probe.get("/boom")
    def boom():
        raise AppError("SOMETHING", 409, "충돌")

    body = TestClient(probe).get("/boom").json()
    assert body["error"]["details"] == {}
