from unittest.mock import ANY, AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api import dev
from app.config import settings
from app.core.security import get_current_user
from app.main import create_app
from app.services import feedback, slack_notify

OPERATOR = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "enable_dev_routes", True)
    monkeypatch.setattr(settings, "dev_operator_user_ids", OPERATOR)
    monkeypatch.setattr(settings, "openai_api_key", "synthetic")
    monkeypatch.setattr(settings, "revenuecat_webhook_auth", "synthetic")
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: OPERATOR
    monkeypatch.setattr(feedback, "_translate", AsyncMock(return_value="합성 번역"))
    monkeypatch.setattr(slack_notify, "send_feedback", AsyncMock(side_effect=AssertionError("no Slack")))
    return TestClient(app)


@pytest.mark.parametrize("case", ["ko", "en", "ja", "mixed", "instruction"])
def test_fixed_cases_use_real_adapter_contract_without_slack(client, case):
    response = client.post("/dev/feedback-translation-eval", json={"case_id": case})
    assert response.status_code == 200
    body = response.json()
    assert body["case_id"] == case and body["configured_model"] == settings.model_utility
    assert body["deployment_sha"] == settings.git_sha
    assert body["runtime_metadata"] == {"chat_model": settings.model_chat,
                                        "embedding_model": settings.embedder_model,
                                        "llm_max_tokens": settings.llm_max_tokens}
    assert "user_id" not in body
    if case == "ko":
        assert body["status"] == "skipped" and body["translation"] is None
        assert body["timings"]["phase"] == "not_started"
        assert body["timings"]["provider_ms"] is None
        feedback._translate.assert_not_awaited()
    else:
        assert body["status"] == "translated"
        feedback._translate.assert_awaited_once_with(dev._FEEDBACK_TRANSLATION_CASES[case],
            experiment_id=dev._FEEDBACK_TRANSLATION_EXPERIMENT, timings=ANY)
    slack_notify.send_feedback.assert_not_awaited()


@pytest.mark.parametrize("payload", [{"case_id": "arbitrary"}, {"case_id": "en", "message": "arbitrary"}])
def test_arbitrary_source_is_rejected(client, payload):
    assert client.post("/dev/feedback-translation-eval", json=payload).status_code == 422
    feedback._translate.assert_not_awaited()


def test_operator_guard_and_no_fallback_success(client, monkeypatch):
    monkeypatch.setattr(settings, "dev_operator_user_ids", "")
    assert client.post("/dev/feedback-translation-eval", json={"case_id": "en"}).status_code == 403
    feedback._translate.assert_not_awaited()
    monkeypatch.setattr(settings, "dev_operator_user_ids", OPERATOR)
    feedback._translate.return_value = None
    assert client.post("/dev/feedback-translation-eval", json={"case_id": "en"}).json()["status"] == "fallback"


@pytest.mark.parametrize("environment", ["local", "production", "staging", "unexpected"])
def test_environment_guard(client, monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    assert client.post("/dev/feedback-translation-eval", json={"case_id": "en"}).status_code == 403
    feedback._translate.assert_not_awaited()


def test_production_app_does_not_register_eval(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    assert TestClient(create_app()).post("/dev/feedback-translation-eval", json={"case_id": "en"}).status_code == 404


def test_sdk_error_body_never_becomes_response(client):
    feedback._translate.side_effect = RuntimeError("private-provider-body")
    response = client.post("/dev/feedback-translation-eval", json={"case_id": "en"})
    assert response.status_code == 200 and response.json()["status"] == "fallback"
    assert "private-provider-body" not in response.text


def test_unapproved_model_is_rejected_before_provider_call(client, monkeypatch):
    monkeypatch.setattr(settings, "model_utility", "gpt-different-price")
    response = client.post("/dev/feedback-translation-eval", json={"case_id": "en"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "EVAL_MODEL_MISMATCH"
    feedback._translate.assert_not_awaited()


@pytest.mark.parametrize("key,value", [("model_chat", "https://private.invalid/key"),
    ("embedder_model", "https://private.invalid/key"),
    ("embedder_model", "private secret"), ("llm_max_tokens", True),
    ("llm_max_tokens", "1024"), ("llm_max_tokens", 0), ("llm_max_tokens", -1)])
def test_runtime_metadata_is_validated_before_provider_call(client, monkeypatch, key, value):
    monkeypatch.setattr(settings, key, value)
    response = client.post("/dev/feedback-translation-eval", json={"case_id": "en"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVAL_RUNTIME_CONFIG_INVALID"
    assert "private" not in response.text
    feedback._translate.assert_not_awaited()


def test_chat_runtime_model_is_distinct_from_utility_model(client, monkeypatch):
    monkeypatch.setattr(settings, "model_chat", "gpt-another-chat-model")
    response = client.post("/dev/feedback-translation-eval", json={"case_id": "ko"})
    assert response.status_code == 200
    assert response.json()["configured_model"] == settings.model_utility
    assert response.json()["runtime_metadata"]["chat_model"] == "gpt-another-chat-model"
    feedback._translate.assert_not_awaited()
