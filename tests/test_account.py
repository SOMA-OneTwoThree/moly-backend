"""계정 API 조립·검증·라우팅(오버라이드로 DB 없이)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.services import account as account_service
from app.services.account import _uid, assemble_me


def test_assemble_me_shape():
    profile = SimpleNamespace(
        nickname="지우", timezone="Asia/Seoul", language="ko", hay_balance=640
    )
    me = assemble_me(profile, {"plan": "free"}, {"head": "item-1"})
    assert me["profile"] == {
        "nickname": "지우",
        "timezone": "Asia/Seoul",
        "language": "ko",
        "onboarded": True,
    }
    assert me["wallet"] == {"balance": 640}
    assert me["equipment"] == {
        "background_id": None,
        "head_id": "item-1",
        "neck_id": None,
        "body_id": None,
    }


def test_assemble_me_onboarded_false_when_no_nickname():
    profile = SimpleNamespace(nickname=None, timezone="UTC", language="ko", hay_balance=0)
    assert assemble_me(profile, {}, {})["profile"]["onboarded"] is False


async def _dummy_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: "11111111-1111-1111-1111-111111111111"
    app.dependency_overrides[get_session] = _dummy_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_get_me_wired(client, monkeypatch):
    async def _fake_get_me(session, user_id):
        return {"profile": {"onboarded": True}, "entitlement": {"plan": "trial"}}

    monkeypatch.setattr(account_service, "get_me", _fake_get_me)
    r = client.get("/me")
    assert r.status_code == 200
    assert r.json()["entitlement"]["plan"] == "trial"


def test_onboarding_nickname_too_long_returns_422(client):
    r = client.post(
        "/onboarding",
        json={"nickname": "12345678901", "timezone": "Asia/Seoul", "language": "ko"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION"


def test_onboarding_wired(client, monkeypatch):
    async def _fake_onboarding(session, user_id, req):
        return {"profile": {"nickname": req.nickname}, "entitlement": {"plan": "trial"}}

    monkeypatch.setattr(account_service, "onboarding", _fake_onboarding)
    r = client.post(
        "/onboarding",
        json={"nickname": "지우", "timezone": "Asia/Seoul", "language": "ko"},
    )
    assert r.status_code == 200
    assert r.json()["profile"]["nickname"] == "지우"


def test_me_requires_auth_returns_401():
    # get_current_user는 실제(인증 없음 → 401), 세션만 더미(빈 DSN 회피)
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).get("/me")  # Authorization 헤더 없음
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_patch_me_wired(client, monkeypatch):
    async def _fake_patch(session, user_id, req):
        return {"profile": {"nickname": req.nickname, "language": req.language}}

    monkeypatch.setattr(account_service, "patch_me", _fake_patch)
    r = client.patch("/me", json={"nickname": "민수"})
    assert r.status_code == 200
    assert r.json()["profile"]["nickname"] == "민수"


def test_patch_me_invalid_timezone_returns_422(client):
    # 서비스 미모킹 → 실제 patch_me의 _validate_timezone이 먼저 실행(DB 접근 전 422)
    r = client.patch("/me", json={"timezone": "Not/AZone"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION"


def test_patch_me_nickname_too_long_returns_422(client):
    r = client.patch("/me", json={"nickname": "12345678901"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION"


def test_onboarding_rejects_unknown_field(client):
    # extra="forbid" — 서버 전용 필드 주입 시도 차단
    r = client.post(
        "/onboarding",
        json={
            "nickname": "지우",
            "timezone": "Asia/Seoul",
            "language": "ko",
            "hay_balance": 999999,
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION"


def test_uid_invalid_raises_unauthorized():
    with pytest.raises(AppError) as e:
        _uid("not-a-uuid")
    assert e.value.code == "UNAUTHORIZED"
    assert e.value.http_status == 401
