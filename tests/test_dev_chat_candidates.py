"""Candidate evaluation is bounded and operator-only; no provider or DB calls."""
from __future__ import annotations

import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import dev
from app.core import errors
from app.services.model_eval import EvalResult


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dev.settings, "environment", "development")
    monkeypatch.setattr(dev.settings, "git_sha", "a" * 40)
    app = FastAPI()
    errors.register_error_handlers(app)
    app.include_router(dev.router)
    app.dependency_overrides[dev.require_dev_operator] = lambda: "operator"
    return TestClient(app)


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def fake(provider, model, system, messages, *, max_tokens):
        seen.append((provider, model, system, messages, max_tokens))
        return EvalResult(provider, model, "reply", 1, 12, 3, 0.00001, finish_reason="stop")

    monkeypatch.setattr(dev.model_eval, "run_eval", fake)
    return seen


def payload(**overrides):
    return {
        "provider": "openai", "model": "gpt-6-luna",
        "messages": [{"role": "user", "content": "안녕"}],
        "language": "ko", "candidate_id": "c0", **overrides,
    }


def test_absent_candidate_preserves_existing_persona_options(client, calls, monkeypatch):
    monkeypatch.setattr(dev.settings, "environment", "local")
    req = payload(language="en", use_persona=False, max_tokens=4096, model="other-model")
    del req["candidate_id"]
    response = client.post("/dev/chat-eval", json=req)
    assert response.status_code == 200
    assert calls == [("openai", "other-model", "", req["messages"], 4096)]
    assert "candidate" not in response.json()


def test_candidate_uses_actual_system_and_returns_only_fingerprints(client, calls):
    response = client.post("/dev/chat-eval", json=payload())
    assert response.status_code == 200
    expected_system, version = dev.eval_system_prompt("ko", "c0")
    assert calls[0][2] == expected_system == dev.system_prompt("ko")
    assert calls[0][3] == [{"role": "user", "content": "안녕"}]
    assert response.json()["candidate"] == {
        "candidate_id": "c0", "version": version,
        "system_sha256": hashlib.sha256(expected_system.encode()).hexdigest(),
        "code_sha": "a" * 40,
    }
    assert "system" not in response.json()


@pytest.mark.parametrize("environment", ["local", "production", "staging", "", "unknown"])
def test_candidates_fail_closed_outside_development(client, calls, monkeypatch, environment):
    monkeypatch.setattr(dev.settings, "environment", environment)
    response = client.post("/dev/chat-eval", json=payload())
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("overrides", [
    {"candidate_id": "unknown"}, {"use_persona": False}, {"language": "fr"},
    {"language": "KO"}, {"max_tokens": 1025}, {"model": "unbounded-model"},
    {"provider": "anthropic", "model": "claude-sonnet-5"},
    {"messages": [{"role": "user", "content": "x" * 24_001}]},
    {"messages": [{"role": "assistant", "content": "reply"}]},
    {"candidate_id": "voice-action-ja-v1", "language": "ko"},
    {"system": "client override"},
])
def test_invalid_candidate_request_never_reaches_provider(client, calls, overrides):
    response = client.post("/dev/chat-eval", json=payload(**overrides))
    assert response.status_code == 422
    assert calls == []
    assert "client override" not in response.text


def test_total_input_bound_includes_server_system(client, calls):
    system, _ = dev.eval_system_prompt("ko", "c0")
    content = "x" * (24_001 - len(system))
    response = client.post("/dev/chat-eval", json=payload(
        messages=[{"role": "user", "content": content}],
    ))
    assert response.status_code == 422
    assert calls == []


@pytest.mark.parametrize("value", ["unknown", "secret-not-a-sha", "A" * 40])
def test_unknown_or_malformed_code_sha_is_not_echoed(client, calls, monkeypatch, value):
    monkeypatch.setattr(dev.settings, "git_sha", value)
    response = client.post("/dev/chat-eval", json=payload())
    assert response.status_code == 200
    assert response.json()["candidate"]["code_sha"] is None
    assert value not in response.text


def test_candidate_compare_reuses_system_and_metadata(client, calls):
    request = payload()
    del request["provider"], request["model"]
    request["models"] = [
        {"provider": "openai", "model": "gpt-6-luna"},
        {"provider": "openai", "model": "gpt-6-sol"},
    ]
    response = client.post("/dev/chat-eval/compare", json=request)
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(calls) == len(results) == 2
    assert calls[0][2:5] == calls[1][2:5]
    assert results[0]["candidate"] == results[1]["candidate"]


@pytest.mark.parametrize("models", [None, [
    {"provider": "openai", "model": "gpt-6-luna"},
    {"provider": "openai", "model": "gpt-6-luna"},
], [
    {"provider": "openai", "model": "gpt-6-luna"},
    {"provider": "openai", "model": "gpt-6-sol"},
    {"provider": "openai", "model": "gpt-5.6-luna"},
]])
def test_candidate_compare_cannot_start_default_four_or_duplicate_models(client, calls, models):
    request = payload()
    del request["provider"], request["model"]
    request["models"] = models
    response = client.post("/dev/chat-eval/compare", json=request)
    assert response.status_code == 422
    assert calls == []


def test_existing_compare_without_candidate_keeps_default_model_set(client, calls):
    response = client.post("/dev/chat-eval/compare", json={
        "messages": [{"role": "user", "content": "hello"}], "use_persona": False,
    })
    assert response.status_code == 200
    assert len(calls) == len(dev.model_eval.DEFAULT_MODELS)
    assert all(call[2] == "" for call in calls)


def test_real_operator_dependency_still_gates_candidate_before_provider(calls, monkeypatch):
    monkeypatch.setattr(dev.settings, "environment", "development")
    monkeypatch.setattr(dev.settings, "dev_operator_user_ids", "")
    app = FastAPI()
    errors.register_error_handlers(app)
    app.include_router(dev.router)
    app.dependency_overrides[dev.get_current_user] = lambda: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    response = TestClient(app).post("/dev/chat-eval", json=payload())
    assert response.status_code == 403
    assert calls == []
