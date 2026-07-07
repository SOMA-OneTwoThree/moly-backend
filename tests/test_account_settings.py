"""계정 부가(알림·기기·탈퇴) — 기본값 로직·라우팅·탈퇴 흐름(DB 없이)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.security import get_current_user
from app.main import app
from app.services import account_settings as ss

UID = "11111111-1111-1111-1111-111111111111"


# --- 알림 기본값 로직(순수, fake 세션) ---
class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self._items


class _FakeSession:
    def __init__(self, items):
        self._items = items

    async def execute(self, stmt):
        return _FakeResult(self._items)


async def test_notifications_default_on_when_no_rows():
    result = await ss.get_notifications(_FakeSession([]), UID)
    assert result == {"morning_diary": True, "evening_chat": True}


async def test_notifications_reflect_stored_off():
    rows = [SimpleNamespace(type="morning_diary", enabled=False)]
    result = await ss.get_notifications(_FakeSession(rows), UID)
    assert result == {"morning_diary": False, "evening_chat": True}


# --- 탈퇴 흐름: auth 삭제 + mem0 병행 둘 다 호출 ---
async def test_delete_account_calls_supabase_and_mem0(monkeypatch):
    calls = []

    async def _fake_user(user_id):
        calls.append(("supabase", user_id))

    async def _fake_mem(user_id):
        calls.append(("mem0", user_id))

    monkeypatch.setattr(ss, "_delete_supabase_user", _fake_user)
    monkeypatch.setattr(ss, "_delete_memories", _fake_mem)
    await ss.delete_account(session=None, user_id=UID)
    assert ("supabase", UID) in calls and ("mem0", UID) in calls


# --- 엔드포인트 라우팅(오버라이드 + 서비스 모킹) ---
async def _dummy_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: UID
    app.dependency_overrides[get_session] = _dummy_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_get_notifications_wired(client, monkeypatch):
    async def _fake(session, user_id):
        return {"morning_diary": True, "evening_chat": False}

    monkeypatch.setattr(ss, "get_notifications", _fake)
    r = client.get("/me/notifications")
    assert r.status_code == 200
    assert r.json() == {"morning_diary": True, "evening_chat": False}


def test_push_token_returns_204(client, monkeypatch):
    async def _fake(session, user_id, req):
        return None

    monkeypatch.setattr(ss, "register_push_token", _fake)
    r = client.post("/me/push-token", json={"token": "apns-abc", "platform": "ios"})
    assert r.status_code == 204


def test_logout_returns_204(client, monkeypatch):
    async def _fake(session, user_id, push_token):
        return None

    monkeypatch.setattr(ss, "logout_device", _fake)
    r = client.post("/auth/logout", json={"push_token": "apns-abc"})
    assert r.status_code == 204


def test_delete_me_returns_204(client, monkeypatch):
    async def _fake(session, user_id):
        return None

    monkeypatch.setattr(ss, "delete_account", _fake)
    r = client.delete("/me")
    assert r.status_code == 204


def test_push_token_requires_token(client):
    r = client.post("/me/push-token", json={"platform": "ios"})  # token 누락
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION"
