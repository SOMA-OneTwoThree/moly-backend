"""Single-model eval exposes bounded runtime facts without changing inference."""
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import dev
from app.config import settings
from app.core import errors
from app.core.security import get_current_user
from app.main import create_app
from app.services.model_eval import EvalResult
from app.services.prompts import system_prompt

SHA = "a" * 40
OPERATOR = "00000000-0000-4000-8000-000000000001"
PAYLOAD = {
    "provider": "openai", "model": "gpt-6-sol", "language": "ko",
    "messages": [{"role": "assistant", "content": "먼저 한 말"}, {"role": "user", "content": "응"}],
    "use_persona": True, "max_tokens": 384,
}


@pytest.fixture
def runtime(monkeypatch):
    for key, value in {
        "environment": "development", "git_sha": SHA, "model_chat": "gpt-6-luna",
        "embedder_model": "text-embedding-3-small", "llm_max_tokens": 1024,
        "dev_operator_user_ids": OPERATOR,
    }.items():
        monkeypatch.setattr(settings, key, value)


@pytest.fixture
def result():
    return EvalResult(
        provider="openai", model="gpt-6-sol", text="응답", latency_ms=10,
        input_tokens=100, output_tokens=3, est_cost_usd=0.00023,
        finish_reason="stop", model_snapshot="gpt-6-sol-provider-snapshot",
    )


@pytest.fixture
def inference(monkeypatch, result):
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(dev.model_eval, "run_eval", call)
    return call


@pytest.fixture
def client(runtime):
    app = FastAPI()
    errors.register_error_handlers(app)
    app.include_router(dev.router)
    app.dependency_overrides[get_current_user] = lambda: OPERATOR
    return TestClient(app)


@pytest.mark.parametrize("language,use_persona", [("ko", True), ("ja", True), ("en", True), ("en", False)])
def test_success_keeps_payload_and_distinguishes_models(client, inference, result, language, use_persona):
    payload = {**PAYLOAD, "language": language, "use_persona": use_persona}
    response = client.post("/dev/chat-eval", json=payload)

    assert response.status_code == 200
    assert response.json() == {
        **result.__dict__, "deployment_sha": SHA,
        "runtime_metadata": {
            "chat_model": "gpt-6-luna", "embedding_model": "text-embedding-3-small", "llm_max_tokens": 1024,
        },
    }
    inference.assert_awaited_once_with(
        "openai", "gpt-6-sol", system_prompt(language) if use_persona else "",
        PAYLOAD["messages"], max_tokens=384,
    )


def test_runtime_snapshot_is_captured_before_inference(client, inference, result, monkeypatch):
    async def change_settings(*args, **kwargs):
        monkeypatch.setattr(settings, "model_chat", "gpt-6-sol")
        monkeypatch.setattr(settings, "git_sha", "b" * 40)
        return result

    inference.side_effect = change_settings
    response = client.post("/dev/chat-eval", json=PAYLOAD)
    assert response.status_code == 200
    assert response.json()["deployment_sha"] == SHA
    assert response.json()["runtime_metadata"]["chat_model"] == "gpt-6-luna"


@pytest.mark.parametrize("key,value", [
    ("git_sha", "private-setting-marker"), ("git_sha", "unknown"), ("git_sha", True),
    ("git_sha", "a" * 40 + "\n"), ("git_sha", "A" * 40),
    ("model_chat", "private-setting-marker"), ("model_chat", True),
    ("embedder_model", "private-setting-marker"), ("embedder_model", None),
    ("llm_max_tokens", True), ("llm_max_tokens", "1024"),
    ("llm_max_tokens", 1024.0), ("llm_max_tokens", 0), ("llm_max_tokens", -1),
])
def test_invalid_runtime_is_safe_and_spends_no_call(client, inference, monkeypatch, caplog, key, value):
    monkeypatch.setattr(settings, key, value)
    response = client.post("/dev/chat-eval", json=PAYLOAD)

    assert response.status_code == 503
    assert response.json() == {"error": {
        "code": "DEV_RUNTIME_METADATA_UNAVAILABLE",
        "message": "Development runtime metadata is unavailable.", "details": {},
    }}
    assert "private-setting-marker" not in response.text + caplog.text
    assert "ValidationError" not in caplog.text
    inference.assert_not_awaited()


def test_local_unknown_is_reported_without_fabricating_a_sha(client, inference, monkeypatch):
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "git_sha", "unknown")
    response = client.post("/dev/chat-eval", json=PAYLOAD)
    assert response.status_code == 200
    assert response.json()["deployment_sha"] == "unknown"
    inference.assert_awaited_once()


def test_provider_failure_keeps_existing_response(client, inference):
    failed = EvalResult("openai", "gpt-6-sol", None, 10, 0, 0, None, error="test provider failure")
    inference.return_value = failed
    response = client.post("/dev/chat-eval", json=PAYLOAD)
    assert response.status_code == 200
    assert response.json() == failed.__dict__
    inference.assert_awaited_once()


def test_compare_contract_and_calls_do_not_depend_on_runtime_metadata(client, inference, result, monkeypatch):
    monkeypatch.setattr(settings, "git_sha", "unknown")
    response = client.post("/dev/chat-eval/compare", json={
        "messages": PAYLOAD["messages"], "language": "en", "use_persona": False,
        "models": [{"provider": "openai", "model": "gpt-6-sol"}], "max_tokens": 384,
    })
    assert response.status_code == 200
    assert response.json() == {"results": [result.__dict__]}
    inference.assert_awaited_once_with("openai", "gpt-6-sol", "", PAYLOAD["messages"], max_tokens=384)


def test_authentication_still_required(client, inference):
    client.app.dependency_overrides.clear()
    assert client.post("/dev/chat-eval", json=PAYLOAD).status_code == 401
    inference.assert_not_awaited()


def test_operator_still_required(client, inference):
    client.app.dependency_overrides[get_current_user] = lambda: "00000000-0000-4000-8000-000000000002"
    assert client.post("/dev/chat-eval", json=PAYLOAD).status_code == 403
    inference.assert_not_awaited()


@pytest.mark.parametrize("environment,enabled", [("production", True), ("staging", True), ("development", False)])
def test_dev_route_gates_are_preserved(runtime, inference, monkeypatch, environment, enabled):
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "enable_dev_routes", enabled)
    monkeypatch.setattr(settings, "revenuecat_webhook_auth", "test-key")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: OPERATOR
    assert TestClient(app).post("/dev/chat-eval", json=PAYLOAD).status_code == 404
    inference.assert_not_awaited()
