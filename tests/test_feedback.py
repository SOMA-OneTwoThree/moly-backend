"""feedback 서비스·엔드포인트 — 저장·인증(DB mock)."""
import uuid

from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.security import get_current_user
from app.main import app
from app.schemas.feedback import CreateFeedbackRequest
from app.services import feedback as feedback_service

UID = "11111111-1111-1111-1111-111111111111"
UID_UUID = uuid.UUID(UID)


class FakeSession:
    def __init__(self, profile=None):
        self.added = []
        self.committed = False
        self._profile = profile

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def get(self, model, pk):
        return self._profile


# --- 서비스 ---
async def test_create_feedback_persists_message_and_contact():
    session = FakeSession()
    req = CreateFeedbackRequest(message="버그가 있어요", contact="@moly")
    await feedback_service.create_feedback(session, UID, req)
    assert session.committed is True
    assert len(session.added) == 1
    row = session.added[0]
    assert row.user_id == UID_UUID
    assert row.message == "버그가 있어요"
    assert row.contact == "@moly"


async def test_create_feedback_allows_null_contact():
    session = FakeSession()
    req = CreateFeedbackRequest(message="의견만 남겨요")
    await feedback_service.create_feedback(session, UID, req)
    assert session.added[0].contact is None
    assert session.committed is True


# --- 엔드포인트 ---
async def _dummy_session():
    yield None


def test_create_feedback_requires_auth():
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).post("/feedback", json={"message": "안녕"})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


def test_create_feedback_rejects_blank_message():
    app.dependency_overrides[get_current_user] = lambda: UID
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).post("/feedback", json={"message": ""})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"


def test_create_feedback_returns_204():
    fake = FakeSession()

    async def _session():
        yield fake

    app.dependency_overrides[get_current_user] = lambda: UID
    app.dependency_overrides[get_session] = _session
    try:
        r = TestClient(app).post("/feedback", json={"message": "좋아요", "contact": "a@b.com"})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 204
    assert fake.committed is True and fake.added[0].contact == "a@b.com"


# --- 슬랙 알림 (SOMA-337) ---
def test_feedback_text_format():
    from datetime import datetime

    from app.services import slack_notify

    when = datetime(2026, 7, 23, 1, 30)
    t = slack_notify.feedback_text("uid-1", "지아", "버그가 있어요", "contact@x", when=when)
    assert "버그가 있어요" in t and "contact@x" in t and "지아" in t and "uid-1" in t
    assert "2026-07-23 01:30" in t  # 한국 시각(라벨 없이)
    # 연락처 없으면 그 줄 생략, 닉네임 없으면 표기
    t2 = slack_notify.feedback_text("uid-2", None, "의견만 남겨요", None, when=when)
    assert "연락처" not in t2 and "(닉네임 없음)" in t2 and "의견만 남겨요" in t2


def test_create_feedback_triggers_slack(monkeypatch):
    from types import SimpleNamespace

    fake = FakeSession(profile=SimpleNamespace(nickname="지아"))
    calls: list[str] = []

    async def _spy(text):
        calls.append(text)

    monkeypatch.setattr("app.services.slack_notify.send_summary", _spy)

    async def _session():
        yield fake

    app.dependency_overrides[get_current_user] = lambda: UID
    app.dependency_overrides[get_session] = _session
    try:
        r = TestClient(app).post("/feedback", json={"message": "버그요", "contact": "@x"})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 204
    # BackgroundTasks가 응답 후 슬랙 전송 1건 실행 — 내용·연락처·닉네임·유저id 포함
    assert len(calls) == 1
    assert "버그요" in calls[0] and "@x" in calls[0] and "지아" in calls[0] and UID in calls[0]
