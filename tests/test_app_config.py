"""에러 응답 형식(AppError) 검증. (GET /app-config 엔드포인트는 제거 — Firebase 이관)"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import AppError, insufficient_hay, register_error_handlers


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
