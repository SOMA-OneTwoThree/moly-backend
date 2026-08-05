"""정규화 기억 공개 API — 인증·확인·predicate 계약과 서비스 위임."""

from fastapi.testclient import TestClient

from app.core.db import get_session
from app.main import app

UID = "11111111-1111-1111-1111-111111111111"


async def _dummy_session():
    yield None


def test_memory_requires_authentication():
    app.dependency_overrides[get_session] = _dummy_session
    try:
        response = TestClient(app).get("/memory")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401
