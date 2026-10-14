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
    assert "prototype" not in response.json()


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


@pytest.fixture
def protocol_calls(monkeypatch):
    seen = []

    async def run(candidate_id, model, system, messages, *, max_tokens, language):
        seen.append((candidate_id, model, system, messages, max_tokens, language))
        return {
            **EvalResult("openai", model, "답", 1, 10, 2, 0.00001).__dict__,
            "prototype": {"sdk_create_calls": 1, "raw_json": '{"text":"답"}'},
        }

    monkeypatch.setattr(dev.eval_reply_protocol, "run", run)
    return seen


def test_protocol_dispatch_is_registered_and_diagnostics_are_only_on_new_response(
    client, calls, protocol_calls,
):
    request = payload(candidate_id="voice-speech-ko-v4", max_tokens=192)
    response = client.post("/dev/chat-eval", json=request)
    assert response.status_code == 200
    assert calls == [] and len(protocol_calls) == 1
    assert protocol_calls[0][2] == dev.eval_system_prompt("ko", "voice-combined-v3")[0]
    assert response.json()["prototype"]["sdk_create_calls"] == 1
    assert response.json()["candidate"]["version"] == "cappy-voice-20261014-v4"
    assert response.json()["candidate"]["code_sha"] == "a" * 40
    old = client.post("/dev/chat-eval", json=payload())
    assert "prototype" not in old.json()


@pytest.mark.parametrize("overrides", [
    {"model": "gpt-6-sol"}, {"language": "ja"}, {"language": "en"},
    {"max_tokens": 193}, {"response_format": {"type": "text"}},
    {"openai_api_key": "untrusted"},
])
def test_protocol_rejects_model_locale_cap_and_arbitrary_format_or_key(
    client, calls, protocol_calls, overrides,
):
    request = payload(candidate_id="voice-speech-ko-v4", max_tokens=192)
    request.update(overrides)
    response = client.post("/dev/chat-eval", json=request)
    assert response.status_code == 422
    assert calls == [] and protocol_calls == []


def test_protocol_input_bound_counts_utf8_envelope_and_schema(client, protocol_calls):
    request = payload(candidate_id="voice-act-focus-ko-v4", max_tokens=192)
    system, _ = dev.eval_system_prompt("ko", request["candidate_id"])
    available = 24_000 - dev.eval_reply_protocol.input_bytes(system, request["messages"], request["candidate_id"])
    request["messages"][0]["content"] = "가" * (available // 3 + 20)
    response = client.post("/dev/chat-eval", json=request)
    assert response.status_code == 422 and protocol_calls == []


def test_protocol_requires_known_full_code_sha(client, protocol_calls, monkeypatch):
    monkeypatch.setattr(dev.settings, "git_sha", "a" * 7)
    response = client.post("/dev/chat-eval", json=payload(
        candidate_id="voice-plain-ko-v4", max_tokens=192,
    ))
    assert response.status_code == 422 and protocol_calls == []


@pytest.mark.parametrize("candidate", ["voice-base-ko-v5", "voice-free-ko-v5"])
def test_v5_dispatch_uses_retry_free_plain_registered_system(client, calls, protocol_calls, candidate):
    response = client.post("/dev/chat-eval", json=payload(candidate_id=candidate, max_tokens=192))
    assert response.status_code == 200
    assert calls == [] and len(protocol_calls) == 1
    assert protocol_calls[0][2] == dev.eval_system_prompt("ko", candidate)[0]
    assert response.json()["candidate"]["version"] == "cappy-voice-20261014-v5"
    assert response.json()["candidate"]["code_sha"] == "a" * 40
    assert dev.eval_reply_protocol.response_format(candidate) is None
    response = client.post("/dev/chat-eval", json=payload(
        candidate_id=candidate, max_tokens=192, language="ja",
    ))
    assert response.status_code == 422 and len(protocol_calls) == 1


@pytest.mark.parametrize("candidate,model", [
    ("voice-luna-none-ko-v6", "gpt-6-luna"),
    ("voice-luna-low-ko-v6", "gpt-6-luna"),
    ("voice-sol-none-ko-v6", "gpt-6-sol"),
])
def test_v6_api_enforces_registered_target_and_1024_cap(client, calls, protocol_calls, candidate, model):
    request = payload(candidate_id=candidate, model=model, max_tokens=1024)
    result = client.post("/dev/chat-eval", json=request)
    assert result.status_code == 200 and calls == [] and len(protocol_calls) == 1
    assert result.json()["candidate"]["version"] == "cappy-voice-20261014-v6"
    assert protocol_calls[0][2] == dev.eval_system_prompt("ko", "voice-free-ko-v5")[0]
    for overrides in ({"max_tokens": 192}, {"model": "gpt-6-sol" if model == "gpt-6-luna"
                                           else "gpt-6-luna"}, {"language": "en"}):
        result = client.post("/dev/chat-eval", json={**request, **overrides})
        assert result.status_code == 422 and len(protocol_calls) == 1


@pytest.mark.parametrize("candidate", ["voice-base-ko-v7", "voice-example-ko-v7"])
def test_v7_api_enforces_same_fixed_luna_192_plain_profile(client, calls, protocol_calls, candidate):
    request = payload(candidate_id=candidate, max_tokens=192)
    response = client.post("/dev/chat-eval", json=request)
    assert response.status_code == 200 and calls == [] and len(protocol_calls) == 1
    assert response.json()["candidate"]["version"] == "cappy-voice-20261014-v7"
    assert protocol_calls[0][2] == dev.eval_system_prompt("ko", candidate)[0]
    for overrides in ({"max_tokens": 1024}, {"model": "gpt-6-sol"}, {"language": "ja"}):
        response = client.post("/dev/chat-eval", json={**request, **overrides})
        assert response.status_code == 422 and len(protocol_calls) == 1
