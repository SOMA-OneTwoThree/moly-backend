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
from app.services import relationship_profile, relationship_profile_repo
from app.services.gating import Gating
from app.services.llm import LLMResult

UID = "11111111-1111-1111-1111-111111111111"


# --- Fake async session ---
class _Scalars:
    def __init__(self, items):
        self._items = items

    def __iter__(self):  # 실제 ScalarResult는 iterable — config_store.get_config_values가 그렇게 쓴다
        return iter(self._items)

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _Scalars(self._items)

    def scalar(self):
        return self._items[0] if self._items else None

    def first(self):  # 실제 Result.first()와 같게 — RETURNING 0행이면 None
        return self._items[0] if self._items else None


class FakeSession:
    def __init__(self, get_map=None, execute_items=None):
        self.get_map = get_map or {}
        self.execute_items = execute_items or []
        self.added = []
        self.deleted = []
        self.committed = False

    async def get(self, model, key, **kwargs):
        return self.get_map.get(model.__name__)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        for i, o in enumerate(self.added, start=1):
            if isinstance(o, Message) and o.id is None:
                o.id = i

    async def execute(self, stmt, params=None):
        return _Result(self.execute_items)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def refresh(self, obj):
        pass


def _gating(**over):
    base = dict(
        profile=SimpleNamespace(language="ko", nickname="지훈", review_prompted_at=None, id=UID),
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
    async def _fake_llm(system, convo, **kw):
        return LLMResult(text="그냥 그랬어.", input_tokens=10, output_tokens=20)

    monkeypatch.setattr(llm_module, "generate", _fake_llm)


async def test_post_message_flow(monkeypatch, patched):
    async def _res(session, user_id):
        return _gating()

    monkeypatch.setattr(gating_module, "resolve", _res)
    session = FakeSession()
    req = SimpleNamespace(text="오늘 어땠어?", greeting_id=None)
    out = await chat_service.post_message(session, UID, req, "idem-1")
    assert out.reply.content == "그냥 그랬어."
    # billable = ceil(10 + 5*20 + 0.1*(0+0)) = 110 (원가 가중: 출력 5×, 캐시 0.1×)
    assert out.tokens_used == 1110  # 1000 + 110
    assert out.tokens_remaining == 98_890  # 100000 - 1110
    assert out.review_prompt is False
    assert session.committed is True


async def test_post_message_stores_placeholder_and_renders_egress(monkeypatch):
    # 불변식: 저장(messages.content)엔 실이름 0·placeholder만, 클라 응답엔 현재 이름 렌더.
    async def _res(session, user_id):
        return _gating()

    async def _fake_llm(system, convo, **kw):
        return LLMResult(text="지훈아 반가워.", input_tokens=10, output_tokens=20)

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(llm_module, "generate", _fake_llm)
    session = FakeSession()
    req = SimpleNamespace(text="나는 지훈이야", greeting_id=None)
    out = await chat_service.post_message(session, UID, req, "idem-ph")

    assert out.reply.content == "지훈아 반가워."  # egress 렌더
    stored = [m for m in session.added if isinstance(m, Message)]
    user_msg = next(m for m in stored if m.sender == "user")
    capi_msg = next(m for m in stored if m.sender == "moly")
    assert "지훈" not in user_msg.content and "{유저이름}" in user_msg.content
    assert "지훈" not in capi_msg.content and "{유저이름}" in capi_msg.content


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
    assert out.review_prompt is True


async def test_post_message_survives_corrupt_relationship_profile(monkeypatch):
    # 저장된 projection 문서가 손상돼도 과거 사본으로 폴백하지 않고 빈 기억으로 대화한다.
    async def _boom(session, *, user_id, language):
        raise relationship_profile.DocumentSchemaError("bad projection")

    async def _fake_llm(system, convo, **kw):
        return LLMResult(text="응 그래.", input_tokens=10, output_tokens=20)

    async def _res(session, user_id):
        return _gating()

    monkeypatch.setattr(relationship_profile_repo, "prompt_text", _boom)
    monkeypatch.setattr(llm_module, "generate", _fake_llm)
    monkeypatch.setattr(gating_module, "resolve", _res)
    req = SimpleNamespace(text="안녕", greeting_id=None)
    out = await chat_service.post_message(FakeSession(), UID, req, "idem-mem")
    assert out.reply.content == "응 그래."


async def test_post_message_fail_closed_when_limit_unresolved(monkeypatch, patched):
    # daily_token_limit 미해석(None) → 무제한 아님, free 한도로 차단 판정
    async def _res(session, user_id):
        return _gating(
            tokens_used=20_000,  # free 20k 소진
            entitlement={
                "plan": "free", "tokens_remaining": None, "daily_token_limit": None,
                "personal_diary_token_threshold": 2000,
            },
        )

    monkeypatch.setattr(gating_module, "resolve", _res)
    req = SimpleNamespace(text="더", greeting_id=None)
    with pytest.raises(AppError) as e:
        await chat_service.post_message(FakeSession(), UID, req, "idem-fc")
    assert e.value.code == "DAILY_LIMIT_REACHED"


async def test_post_message_idempotent_returns_cached(monkeypatch, patched):
    async def _res(session, user_id):
        return _gating()

    monkeypatch.setattr(gating_module, "resolve", _res)
    cached_response = {
        "greeting": None,
        "user_message": {"message_id": "1", "created_at": "2026-07-07T00:00:00+00:00"},
        "reply": {
            "message_id": "2",
            "content": "이전 응답",
            "created_at": "2026-07-07T00:00:00+00:00",
        },
        "tokens_used": 100,
        "tokens_remaining": 19_900,
        "review_prompt": False,
    }
    cached = SimpleNamespace(response=cached_response)
    session = FakeSession(get_map={"IdempotencyKey": cached})
    req = SimpleNamespace(text="재시도", greeting_id=None)
    out = await chat_service.post_message(session, UID, req, "same-key")
    # 와이어 포맷 보존: 저장본과 json 직렬화 결과가 동일해야 한다(+00:00 유지 포함).
    expected = {**cached_response, "reply": {**cached_response["reply"], "references": []}}
    assert out.model_dump(mode="json") == expected
    assert session.committed is False  # LLM·저장 안 탐


async def test_post_message_llm_failure_persists_nothing(monkeypatch):
    # SOMA-374: phase-1은 read-only라 LLM 실패 시 유저 메시지·토큰·멱등키가 저장되지 않는다(클린 재시도).
    from app.models.idempotency_key import IdempotencyKey

    async def _res(session, user_id):
        return _gating()

    async def _boom(*a, **k):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(llm_module, "generate", _boom)
    session = FakeSession()
    req = SimpleNamespace(text="안녕", greeting_id=None)
    with pytest.raises(RuntimeError):
        await chat_service.post_message(session, UID, req, "idem-llmfail")
    assert [m for m in session.added if isinstance(m, Message)] == []
    assert [o for o in session.added if isinstance(o, IdempotencyKey)] == []


async def test_post_message_passes_llm_timeout(monkeypatch, patched):
    # SOMA-374: LLM 호출에 per-request timeout이 전달된다(무한 대기 방지).
    captured: dict = {}

    async def _res(session, user_id):
        return _gating()

    async def _capture(system, convo, **kw):
        captured.update(kw)
        return LLMResult(text="응.", input_tokens=10, output_tokens=20)

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(llm_module, "generate", _capture)
    req = SimpleNamespace(text="hi", greeting_id=None)
    await chat_service.post_message(FakeSession(), UID, req, "idem-to")
    assert captured.get("timeout") is not None


async def test_post_message_phase2_dup_returns_cached(monkeypatch, patched):
    # SOMA-374: LLM 도중 동시 중복이 먼저 확정하면, phase-2 멱등 재조회에서 발견해 저장응답을 반환하고
    # 이중 저장하지 않는다(phase-0엔 없었지만 phase-2엔 존재).
    async def _res(session, user_id):
        return _gating()

    monkeypatch.setattr(gating_module, "resolve", _res)
    cached_response = {
        "greeting": None,
        "user_message": {"message_id": "1", "created_at": "2026-07-07T00:00:00+00:00"},
        "reply": {"message_id": "2", "content": "동시본", "created_at": "2026-07-07T00:00:00+00:00"},
        "tokens_used": 100,
        "tokens_remaining": 19_900,
        "review_prompt": False,
    }

    class DupSession(FakeSession):
        def __init__(self):
            super().__init__()
            self._idem_calls = 0

        async def get(self, model, key, **kwargs):
            if model.__name__ == "IdempotencyKey":
                self._idem_calls += 1
                # phase-0(1회차)=None → 진행 / phase-2(2회차)=cached → 재조회 히트
                return None if self._idem_calls == 1 else SimpleNamespace(response=cached_response)
            return self.get_map.get(model.__name__)

    session = DupSession()
    req = SimpleNamespace(text="hi", greeting_id=None)
    out = await chat_service.post_message(session, UID, req, "dup-key")
    assert out.reply.content == "동시본"
    assert out.reply.references == []
    assert [m for m in session.added if isinstance(m, Message)] == []  # 이중 저장 없음


async def test_post_message_incompatible_cache_fails_closed(monkeypatch):
    async def _must_not_run(*args, **kwargs):
        raise AssertionError("비호환 캐시를 새 채팅으로 재실행하면 안 됨")

    monkeypatch.setattr(gating_module, "resolve", _must_not_run)
    monkeypatch.setattr(llm_module, "generate", _must_not_run)
    cached = SimpleNamespace(response={"reply": {"content": "구형 응답"}})
    session = FakeSession(get_map={"IdempotencyKey": cached})

    # 재시도해도 행은 보존된 채 매번 500 — 지우면 다음 재시도가 새 요청으로 실행되어
    # 메시지·토큰이 중복된다. 정리는 운영 스크립트(--delete-invalid) 전용(api-inventory.md).
    for _ in range(2):
        with pytest.raises(AppError) as exc:
            await chat_service.post_message(
                session,
                UID,
                SimpleNamespace(text="재시도", greeting_id=None),
                "legacy-key",
            )
        assert exc.value.code == "INTERNAL"
        assert exc.value.http_status == 500

    assert session.added == []
    assert session.deleted == []
    assert session.committed is False


async def test_get_state_shape(monkeypatch):
    async def _res(session, user_id):
        return _gating(tokens_used=2500)

    monkeypatch.setattr(gating_module, "resolve", _res)
    out = await chat_service.get_state(FakeSession(), UID)
    assert out["plan"] == "trial"
    assert out["tokens_used"] == 2500
    assert out["personal_diary_eligible"] is True  # 2500 ≥ 2000
    assert out["limit_reached"] is False


class SeqSession(FakeSession):
    """execute 호출마다 다른 결과를 돌려주는 세션 — get_greeting은 락·발화·발급 3번 조회한다."""

    def __init__(self, get_map=None, sequences=None):
        super().__init__(get_map=get_map)
        self._seq = list(sequences or [])

    async def execute(self, stmt, params=None):
        return _Result(self._seq.pop(0) if self._seq else [])


def _greeting_session(*, spoke, issued):
    return SeqSession(
        get_map={"Profile": SimpleNamespace(timezone="Asia/Seoul", language="ko",
                                            nickname="지훈", id=UID)},
        sequences=[[], [1] if spoke else [], ["g-1"] if issued else []],  # 락 · 오늘 발화 · 오늘 발급
    )


async def test_greeting_none_when_user_already_spoke_today():
    """오늘 한 마디라도 했으면 선발화 없음 — 대화 중 난입 방지(하루 1회의 핵심)."""
    session = _greeting_session(spoke=True, issued=False)
    assert await chat_service.get_greeting(session, UID, "home_enter") == {
        "greeting_id": None, "content": None
    }
    assert session.added == []  # 새 인사 발급 안 함


async def test_greeting_none_when_already_issued_today():
    """이미 낸 인사는 다시 안 내준다 — 재진입마다 같은 인사가 또 뜨던 버그."""
    session = _greeting_session(spoke=False, issued=True)
    assert await chat_service.get_greeting(session, UID, "home_enter") == {
        "greeting_id": None, "content": None
    }
    assert session.added == []


async def test_greeting_issued_when_nothing_yet_today(monkeypatch):
    called = {"llm": False}

    async def _fake_llm(system, convo, **kw):
        called["llm"] = True
        return LLMResult("새 인사", 1, 1)

    monkeypatch.setattr(llm_module, "generate", _fake_llm)
    session = _greeting_session(spoke=False, issued=False)
    out = await chat_service.get_greeting(session, UID, "home_enter")
    assert out["content"]                       # 프리셋에서 하나 발급
    assert "지훈" not in session.added[0].content  # 저장은 placeholder만(이름 표면 0)
    assert len(session.added) == 1              # greetings 1건 저장
    assert called["llm"] is False               # 선발화는 코드 프리셋 — LLM 미호출


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


def test_send_rejects_reserved_prefix_key():
    """raw key 저장소를 공유하는 채팅이 상점 네임스페이스를 위장하면 안 된다."""
    app.dependency_overrides[get_current_user] = lambda: UID
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).post(
            "/chat/messages",
            json={"text": "안녕"},
            headers={"Idempotency-Key": "shop-purchase:key"},
        )
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
