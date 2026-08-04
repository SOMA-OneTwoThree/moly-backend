"""정규화 기억 공개 API — 인증·확인·predicate 계약과 서비스 위임."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core import errors
from app.core.db import get_session
from app.core.security import get_current_user
from app.main import app
from app.schemas.memory import MemoryForgetRequest
from app.services import memory_api, memory_forget

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


@pytest.mark.parametrize(
    "body",
    [
        {"scope": "all", "confirm": False},
        {"scope": "fact", "confirm": True},
        {"scope": "predicate", "confirm": True},
        {"scope": "all", "confirm": True, "fact_ids": [UID]},
    ],
)
def test_forget_rejects_unconfirmed_or_incomplete_scope(body):
    app.dependency_overrides[get_current_user] = lambda: UID
    app.dependency_overrides[get_session] = _dummy_session
    try:
        response = TestClient(app).post("/memory/forget", json=body)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION"


async def test_forget_service_rejects_unknown_predicate_before_db_access():
    req = MemoryForgetRequest(scope="predicate", predicate="made_up", confirm=True)
    with pytest.raises(errors.AppError) as caught:
        await memory_api.forget(None, UID, req)
    assert caught.value.code == "VALIDATION"


async def test_forget_service_commits_the_atomic_result(monkeypatch):
    fact_id = uuid.uuid4()
    seen = {}

    class Session:
        committed = False

        async def commit(self):
            self.committed = True

    async def apply(session, *, user_id, request):
        seen.update(user_id=user_id, request=request)
        return memory_forget.ForgetResult(
            status=memory_forget.RESULT_APPLIED,
            request=request,
            memory_generation=4,
            forgotten_facts=(fact_id,),
        )

    monkeypatch.setattr(memory_forget, "apply", apply)
    session = Session()
    req = MemoryForgetRequest(scope="fact", fact_ids=[fact_id], confirm=True)
    result = await memory_api.forget(session, UID, req)

    assert session.committed is True
    assert seen["user_id"] == uuid.UUID(UID)
    assert result["forgotten_fact_ids"] == [fact_id]
    assert result["memory_generation"] == 4
