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
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


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
