"""chat 서비스 — 전송 흐름·토큰집계·게이팅·멱등·상태·선발화 캐시(DB·LLM mock)."""
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.models.message import Message
from app.services import chat as chat_service
from app.services import gating as gating_module
from app.services import llm as llm_module
from app.services import memory as memory_module
from app.services.gating import Gating
from app.services.llm import LLMResult

UID = "11111111-1111-1111-1111-111111111111"


# --- Fake async session ---
class _Scalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _Scalars(self._items)


class FakeSession:
    def __init__(self, get_map=None, execute_items=None):
        self.get_map = get_map or {}
        self.execute_items = execute_items or []
        self.added = []
        self.committed = False

    async def get(self, model, key):
        return self.get_map.get(model.__name__)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for i, o in enumerate(self.added, start=1):
            if isinstance(o, Message) and o.id is None:
                o.id = i

    async def execute(self, stmt):
        return _Result(self.execute_items)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        pass


def _gating(**over):
    base = dict(
        profile=SimpleNamespace(language="ko", review_prompted_at=None, id=UID),
        activity_date=date(2026, 7, 7),
        entitlement={
            "plan": "trial",
            "tokens_remaining": 5000,
            "daily_token_limit": 100_000,
            "personal_diary_token_threshold": 2000,
        },
        tokens_used=1000,
        warning_threshold=3000,
        review_min_tokens=50_000,
    )
    base.update(over)
    return Gating(**base)


@pytest.fixture
def patched(monkeypatch):
    async def _fake_mem(user_id):
        return ""

    async def _fake_llm(system, convo, *, max_tokens=None):
        return LLMResult(text="그냥 그랬어.", input_tokens=10, output_tokens=20)

    monkeypatch.setattr(memory_module, "load_for_context", _fake_mem)
    monkeypatch.setattr(llm_module, "generate", _fake_llm)


async def test_post_message_flow(monkeypatch, patched):
    async def _res(session, user_id):
        return _gating()

    monkeypatch.setattr(gating_module, "resolve", _res)
    session = FakeSession()
    req = SimpleNamespace(text="오늘 어땠어?", greeting_id=None)
    out = await chat_service.post_message(session, UID, req, "idem-1")
    assert out["reply"]["content"] == "그냥 그랬어."
    assert out["tokens_used"] == 1030  # 1000 + (10+20)
    assert out["tokens_remaining"] == 98_970  # 100000 - 1030
    assert out["review_prompt"] is False
    assert session.committed is True


async def test_post_message_daily_limit(monkeypatch, patched):
    async def _res(session, user_id):
        return _gating(entitlement={
            "plan": "free", "tokens_remaining": 0, "daily_token_limit": 20_000,
            "personal_diary_token_threshold": 2000,
        })

    monkeypatch.setattr(gating_module, "resolve", _res)
    req = SimpleNamespace(text="더 얘기하자", greeting_id=None)
    with pytest.raises(AppError) as e:
        await chat_service.post_message(FakeSession(), UID, req, "idem-2")
    assert e.value.code == "DAILY_LIMIT_REACHED"
    assert e.value.http_status == 403


async def test_post_message_review_prompt_crossing_threshold(monkeypatch, patched):
    async def _res(session, user_id):
        return _gating(tokens_used=49_990)  # +30 → 50020 ≥ 50000

    monkeypatch.setattr(gating_module, "resolve", _res)
    req = SimpleNamespace(text="ㅎㅇ", greeting_id=None)
    out = await chat_service.post_message(FakeSession(), UID, req, "idem-3")
    assert out["review_prompt"] is True


async def test_post_message_idempotent_returns_cached(monkeypatch, patched):
    async def _res(session, user_id):
        return _gating()

    monkeypatch.setattr(gating_module, "resolve", _res)
    cached = SimpleNamespace(response={"reply": {"content": "이전 응답"}})
    session = FakeSession(get_map={"IdempotencyKey": cached})
    req = SimpleNamespace(text="재시도", greeting_id=None)
    out = await chat_service.post_message(session, UID, req, "same-key")
    assert out == {"reply": {"content": "이전 응답"}}
    assert session.committed is False  # LLM·저장 안 탐


async def test_get_state_shape(monkeypatch):
    async def _res(session, user_id):
        return _gating(tokens_used=2500)

    monkeypatch.setattr(gating_module, "resolve", _res)
    out = await chat_service.get_state(FakeSession(), UID)
    assert out["plan"] == "trial"
    assert out["tokens_used"] == 2500
    assert out["personal_diary_eligible"] is True  # 2500 ≥ 2000
    assert out["limit_reached"] is False


async def test_greeting_cache_hit(monkeypatch, patched):
    existing = SimpleNamespace(id="g-1", content="왔네.")
    session = FakeSession(
        get_map={"Profile": SimpleNamespace(timezone="Asia/Seoul", language="ko", id=UID)},
        execute_items=[existing],
    )
    called = {"llm": False}

    async def _fake_llm(system, convo, *, max_tokens=None):
        called["llm"] = True
        return LLMResult("새 인사", 1, 1)

    monkeypatch.setattr(llm_module, "generate", _fake_llm)
    out = await chat_service.get_greeting(session, UID, "home_enter")
    assert out == {"greeting_id": "g-1", "content": "왔네."}
    assert called["llm"] is False  # 캐시 → LLM 미호출


# --- 엔드포인트 ---
async def _dummy_session():
    yield None


def test_send_requires_idempotency_key():
    app.dependency_overrides[get_current_user] = lambda: UID
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).post("/chat/messages", json={"text": "안녕"})  # 헤더 없음
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION"


def test_chat_requires_auth():
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).get("/chat/state")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
