"""트레이닝 HTTP 경계 — 인증, 언어 헤더, 요청 본문 검증, 응답 직렬화. 서비스는 바꿔 끼운다."""

from __future__ import annotations

import unicodedata
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.schemas.economy import HayTransactionResponse
from app.services import trainings

USER = "00000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
TRIES = {"left": 4, "max": 5, "next_refill_at": NOW, "full_at": NOW}
PROGRESS = {"next_stage": 2, "passed_count": 1, "completed": False}
RESULT = {
    "result": "passed", "judge": "ai", "hint": None, "explanation": None, "reward": {"granted": 15, "balance_after": 115},
    "answer": {"stage": 1, "option_id": None, "text": "포장 실수", "passed_at": NOW},
    "progress": PROGRESS, "tries": TRIES,
}

ROUTES = [
    ("GET", "/trainings"),
    ("GET", "/trainings/practice-basics"),
    ("POST", "/trainings/practice-basics/stages/1/answers"),
    ("POST", "/trainings/practice-basics/stages/1/claims"),
]


async def _no_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_session] = _no_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_routes_require_authentication(method, path):
    assert TestClient(app).request(method, path, json={}).status_code == 401


@pytest.mark.parametrize(
    ("sent", "expected"), [(None, None), ("ko", "ko"), ("ko-KR", "ko"), ("EN-us", "en"), ("ja-JP", "ja")]
)
def test_locale_header_is_normalized(client, monkeypatch, sent, expected):
    captured = []

    async def fake(session, user_id, *, locale):
        captured.append(locale)
        return {"trainings": []}

    monkeypatch.setattr(trainings, "list_trainings", fake)
    headers = {} if sent is None else {"X-App-Locale": sent}
    response = client.get("/trainings", headers=headers)
    assert response.status_code == 200 and response.json() == {"trainings": []}
    assert captured == [expected]


@pytest.mark.parametrize("sent", ["zh-Hant-TW", "jp", "ko_kr"])
def test_unsupported_locale_header_is_rejected(client, monkeypatch, sent):
    async def must_not_run(*args, **kwargs):  # pragma: no cover - 검증이 먼저다
        raise AssertionError("unsupported locale reached the service")

    monkeypatch.setattr(trainings, "list_trainings", must_not_run)
    response = client.get("/trainings", headers={"X-App-Locale": sent})
    assert response.status_code == 422
    assert response.json()["error"]["details"]["errors"][0]["loc"] == ["header", "X-App-Locale"]


@pytest.fixture
def submitted(client, monkeypatch):
    calls = []

    async def fake(session, user_id, training_id, stage, body, *, locale):
        calls.append((training_id, stage, body, locale))
        return RESULT

    monkeypatch.setattr(trainings, "submit", fake)
    return calls


def test_answer_result_is_serialized(client, submitted):
    request_id = str(uuid.uuid4())
    response = client.post(
        "/trainings/practice-basics/stages/1/answers",
        json={"client_request_id": request_id, "text": "포장 실수"},
        headers={"X-App-Locale": "ko-KR"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answer"]["passed_at"] == "2026-10-21T09:00:00+00:00"
    assert body["tries"]["full_at"] == "2026-10-21T09:00:00+00:00"
    assert body["reward"] == {"granted": 15, "balance_after": 115}
    training_id, stage, request, locale = submitted[0]
    assert (training_id, stage, str(request.client_request_id), locale) == ("practice-basics", 1, request_id, "ko")


def test_answer_text_is_trimmed_and_nfc_normalized(client, submitted):
    decomposed = unicodedata.normalize("NFD", "  포장 실수  ")
    client.post(
        "/trainings/practice-basics/stages/1/answers",
        json={"client_request_id": str(uuid.uuid4()), "text": decomposed},
    )
    assert submitted[0][2].text == "포장 실수"


def test_answer_limit_counts_code_points(client, submitted):
    ok = client.post(
        "/trainings/practice-basics/stages/1/answers",
        json={"client_request_id": str(uuid.uuid4()), "text": "가" * 200},
    )
    assert ok.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"text": "답"},
        {"client_request_id": "not-a-uuid", "text": "답"},
        {"client_request_id": "00000000-0000-4000-8000-000000000009"},
        {"client_request_id": "00000000-0000-4000-8000-000000000009", "text": "답", "option_id": "a"},
        {"client_request_id": "00000000-0000-4000-8000-000000000009", "text": "   "},
        {"client_request_id": "00000000-0000-4000-8000-000000000009", "text": "가" * 201},
        {"client_request_id": "00000000-0000-4000-8000-000000000009", "option_id": ""},
        {"client_request_id": "00000000-0000-4000-8000-000000000009", "text": "답", "extra": 1},
    ],
)
def test_invalid_answer_bodies_are_rejected(client, submitted, body):
    response = client.post("/trainings/practice-basics/stages/1/answers", json=body)
    assert response.status_code == 422
    assert submitted == []
    # 제출한 글은 오류 응답에 다시 싣지 않는다.
    assert "가" * 50 not in response.text


def test_no_tries_error_carries_the_refill_times(client, monkeypatch):
    async def blocked(*args, **kwargs):
        raise trainings._no_tries(trainings.training_tries.Tries(0, NOW))

    monkeypatch.setattr(trainings, "submit", blocked)
    response = client.post(
        "/trainings/practice-basics/stages/1/answers",
        json={"client_request_id": str(uuid.uuid4()), "option_id": "a"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TRAINING_NO_TRIES"
    assert response.json()["error"]["details"]["tries"] == {
        "left": 0, "max": 5,
        "next_refill_at": "2026-10-21T10:00:00+00:00", "full_at": "2026-10-21T14:00:00+00:00",
    }


def test_claim_returns_no_content(client, monkeypatch):
    calls = []

    async def fake(session, user_id, training_id, stage, body):
        calls.append(body)

    monkeypatch.setattr(trainings, "claim", fake)
    response = client.post(
        "/trainings/practice-basics/stages/2/claims",
        json={"reason": "wrong_hint", "answer_text": "포장 실수", "verdict": "retry", "hint": "  "},
    )
    assert response.status_code == 204 and response.content == b""
    assert calls[0].hint is None


def test_claim_rejects_unknown_reasons(client, monkeypatch):
    response = client.post(
        "/trainings/practice-basics/stages/2/claims",
        json={"reason": "angry", "answer_text": "포장 실수", "verdict": "retry"},
    )
    assert response.status_code == 422


def test_service_errors_use_the_error_envelope(client, monkeypatch):
    async def missing(*args, **kwargs):
        raise AppError("TRAINING_NOT_FOUND", 404, "트레이닝을 찾을 수 없어요.")

    monkeypatch.setattr(trainings, "get_training", missing)
    response = client.get("/trainings/nope")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "TRAINING_NOT_FOUND", "message": "트레이닝을 찾을 수 없어요.", "details": {}}
    }


def test_training_reward_transactions_pass_the_wallet_contract():
    tx = HayTransactionResponse(id="7", type="training_reward", amount=15, balance_after=115, created_at=NOW)
    assert tx.type == "training_reward"
