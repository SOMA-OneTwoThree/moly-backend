"""모든 에러가 표준 형식 {error:{code,message,details}}로 통일되는지 검증."""
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.errors import register_error_handlers
from app.core.security import get_current_user


def _probe_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/protected")
    async def protected(uid: str = Depends(get_current_user)):
        return {"uid": uid}

    @app.get("/crash")
    def crash():
        raise ValueError("boom")

    return app


def test_401_uses_envelope_with_code():
    # 인증 헤더 없음 → get_current_user가 UNAUTHORIZED AppError → 표준 봉투
    r = TestClient(_probe_app()).get("/protected")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_404_uses_envelope_with_code():
    r = TestClient(_probe_app()).get("/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_500_uses_envelope_without_leaking_detail():
    client = TestClient(_probe_app(), raise_server_exceptions=False)
    r = client.get("/crash")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "INTERNAL"
    # 내부 예외 메시지("boom")가 새어나가지 않아야 함
    assert "boom" not in body["error"]["message"]
