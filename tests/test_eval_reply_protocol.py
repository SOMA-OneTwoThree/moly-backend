"""Development-only wire-format experiment: one SDK send and honest failures."""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI
import pytest

from app.services import eval_reply_protocol as protocol
from app.services import llm, prompts

PLAIN, SPEECH, ACT = protocol.V4_PROTOCOL_IDS
SYSTEM = prompts.eval_system_prompt("ko", PLAIN)[0]
MESSAGES = [{"role": "user", "content": "합성 입력"}]
_FROZEN_KO_V3_SHA = "dc420452352b663a24466db1c4d6c40e16e07e58f00d9212e9232ef3dcbe838e"


def completion(text, *, finish="stop", refusal=None, usage=True):
    return SimpleNamespace(
        model="gpt-6-luna-test-snapshot",
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(
            content=text, refusal=refusal,
        ))],
        usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=20, prompt_tokens_details=None,
        ) if usage else None,
    )


class Client:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def development(monkeypatch):
    monkeypatch.setattr(protocol.settings, "environment", "development")


async def call(monkeypatch, response, candidate=SPEECH):
    client = Client(response)
    monkeypatch.setattr(llm, "_get_openai_client", lambda: client)
    result = await protocol.run(
        candidate, "gpt-6-luna", SYSTEM, MESSAGES,
        max_tokens=192,
    )
    assert len(client.calls) == 1
    assert client.calls[0]["reasoning_effort"] == "none"
    assert client.calls[0]["max_completion_tokens"] == 192
    assert result["prototype"]["sdk_create_calls"] == 1
    return result, client


@pytest.mark.parametrize("candidate,raw", [
    (PLAIN, "  그대로\n말해.  "),
    (SPEECH, '{"text":"  그대로\\n말해.  "}'),
    (ACT, '{"action":"choice","focus":"창가 자리","text":"  그대로\\n말해.  "}'),
])
async def test_valid_protocol_preserves_decoded_speech_and_actual_usage(monkeypatch, candidate, raw):
    result, client = await call(monkeypatch, completion(raw), candidate)
    assert result["text"] == "  그대로\n말해.  "
    assert result["input_tokens"] == 100 and result["output_tokens"] == 20
    assert result["est_cost_usd"] == 0.00002
    assert result["model_snapshot"] == "gpt-6-luna-test-snapshot"
    assert result["finish_reason"] == "stop"
    assert result["prototype"]["raw_json"] == raw
    assert result["prototype"]["usage_known"] is True
    assert ("response_format" in client.calls[0]) is (candidate != PLAIN)
    expected_hash = hashlib.sha256(json.dumps(
        client.calls[0], ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    assert result["prototype"]["request_sha256"] == expected_hash
    if candidate == ACT:
        assert result["prototype"]["decisions"] == {"action": "choice", "focus": "창가 자리"}


@pytest.mark.parametrize("raw", [
    '{"text":"first","text":"second"}', '{"text":"ok","extra":"hidden"}',
    '{"text":false}', '{"text":null}', '{"text":1}', '{"text":"   "}',
    '[]', '{"text":', '{"text":"' + "가" * 601 + '"}',
])
async def test_invalid_speech_preserves_cost_without_publishing(monkeypatch, raw):
    result, _ = await call(monkeypatch, completion(raw))
    assert result["text"] is None and result["error"] == "ReplyProtocolInvalidResponse"
    assert result["est_cost_usd"] == 0.00002
    assert result["output_tokens"] == 20
    assert result["prototype"]["raw_json"] == raw


@pytest.mark.parametrize("values", [
    {"action": "other", "focus": "점", "text": "답"},
    {"action": False, "focus": "점", "text": "답"},
    {"action": "reply", "focus": "", "text": "답"},
    {"action": "reply", "focus": "가" * 61, "text": "답"},
    {"action": "reply", "focus": ["점"], "text": "답"},
    {"action": "reply", "focus": "점", "text": "답", "rationale": "unused"},
])
async def test_decision_fields_are_compact_and_strict(monkeypatch, values):
    result, _ = await call(monkeypatch, completion(json.dumps(values)), ACT)
    assert result["error"] == "ReplyProtocolInvalidResponse"
    assert result["prototype"]["decisions"] is None
    assert result["est_cost_usd"] == 0.00002


@pytest.mark.parametrize("finish,refusal", [("length", None), ("tool_calls", None), ("", None),
                                        ("stop", "refused")])
async def test_invalid_completion_is_not_a_success_or_a_second_call(monkeypatch, finish, refusal):
    result, _ = await call(monkeypatch, completion('{"text":"답"}', finish=finish, refusal=refusal))
    assert result["text"] is None
    assert result["finish_reason"] == finish
    assert result["est_cost_usd"] == 0.00002


async def test_missing_usage_never_means_free_call(monkeypatch):
    result, _ = await call(monkeypatch, completion('{"text":"답"}', usage=False))
    assert result["text"] == "답"
    assert result["est_cost_usd"] is None
    assert result["prototype"]["usage_known"] is False


async def test_initialization_failure_counts_no_create_and_does_not_log_detail(monkeypatch, caplog):
    def fail():
        raise RuntimeError("synthetic private detail")

    monkeypatch.setattr(llm, "_get_openai_client", fail)
    result = await protocol.run(SPEECH, "gpt-6-luna", SYSTEM, MESSAGES, max_tokens=192)
    assert result["prototype"]["sdk_create_calls"] == 0
    assert result["est_cost_usd"] is None
    assert "synthetic private detail" not in caplog.text
    assert "synthetic private detail" not in json.dumps(result)


async def test_external_cancel_propagates(monkeypatch):
    client = Client(error=asyncio.CancelledError())
    monkeypatch.setattr(llm, "_get_openai_client", lambda: client)
    with pytest.raises(asyncio.CancelledError):
        await protocol.run(SPEECH, "gpt-6-luna", SYSTEM, MESSAGES, max_tokens=192)
    assert len(client.calls) == 1


@pytest.mark.parametrize("status", (429, 500, "read_timeout"))
async def test_installed_sdk_sends_once_and_shared_client_remains_open(monkeypatch, status):
    sent = []

    async def handler(request):
        sent.append(request)
        if status == "read_timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(status, json={"error": {"message": "synthetic", "type": "server_error"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sdk = AsyncOpenAI(api_key="test-only", base_url="https://provider.invalid", http_client=http)
        monkeypatch.setattr(llm, "_get_openai_client", lambda: sdk)
        result = await protocol.run(SPEECH, "gpt-6-luna", SYSTEM, MESSAGES, max_tokens=192)
        assert len(sent) == 1
        assert result["prototype"]["sdk_create_calls"] == 1
        assert result["est_cost_usd"] is None
        assert result["text"] is None
        assert sdk.max_retries == 2
        assert not http.is_closed


@pytest.mark.parametrize("candidate", protocol.V4_PROTOCOL_IDS)
def test_all_protocols_share_frozen_v3_system_and_have_separate_wire_hashes(candidate):
    frozen, _ = prompts.eval_system_prompt("ko", "voice-combined-v3")
    system, version = prompts.eval_system_prompt("ko", candidate)
    assert system == frozen
    assert hashlib.sha256(system.encode()).hexdigest() == _FROZEN_KO_V3_SHA
    assert version == "cappy-voice-20261014-v4"
    for language in ("ja", "en"):
        with pytest.raises(ValueError):
            prompts.eval_system_prompt(language, candidate)
    manifest = protocol.protocol_manifest(candidate, 192)
    assert len(manifest["schema_sha256"]) == 64
    assert manifest["wire_options_sha256"] != protocol.protocol_manifest(candidate, 1024)["wire_options_sha256"]


def test_schema_order_and_conservative_wire_constraints():
    schema = protocol.response_format(ACT)["json_schema"]["schema"]
    assert list(schema["properties"]) == ["action", "focus", "text"]
    assert schema["required"] == ["action", "focus", "text"]
    assert schema["additionalProperties"] is False
    assert "maxLength" not in schema["properties"]["focus"]
    assert len({protocol.protocol_manifest(candidate, 192)["schema_sha256"]
                for candidate in protocol.V4_PROTOCOL_IDS}) == 3


@pytest.mark.parametrize("candidate", ["voice-base-ko-v5", "voice-free-ko-v5"])
async def test_v5_uses_registered_full_system_and_plain_wire_without_extra_call(monkeypatch, candidate):
    system, version = prompts.eval_system_prompt("ko", candidate)
    raw = '  原文 "café"\n다시 말해?  '
    client = Client(completion(raw))
    monkeypatch.setattr(llm, "_get_openai_client", lambda: client)
    result = await protocol.run(candidate, "gpt-6-luna", system, MESSAGES, max_tokens=192)
    assert version == "cappy-voice-20261014-v5"
    assert len(client.calls) == 1
    assert client.calls[0]["messages"][0]["content"] == system
    assert "response_format" not in client.calls[0]
    assert result["text"] == raw
    assert result["prototype"]["decisions"] is None
    assert result["prototype"]["field_order"] is None
    assert result["prototype"]["sdk_create_calls"] == 1
    assert protocol.protocol_manifest(candidate, 192) == protocol.protocol_manifest(PLAIN, 192)
    with pytest.raises(ValueError, match="frozen registered system"):
        await protocol.run(candidate, "gpt-6-luna", system + "override", MESSAGES, max_tokens=192)


@pytest.mark.parametrize("overrides", [
    {"language": "ja"}, {"model": "gpt-6-sol"}, {"system": "arbitrary instructions"},
    {"max_tokens": 193}, {"candidate_id": "unknown"},
    {"messages": [{"role": "system", "content": "override"}]},
])
async def test_internal_adapter_rejects_unregistered_inputs_before_client(monkeypatch, overrides):
    def forbidden():
        raise AssertionError("Invalid experiment reached the client")

    monkeypatch.setattr(llm, "_get_openai_client", forbidden)
    kwargs = dict(candidate_id=SPEECH, model="gpt-6-luna", system=SYSTEM,
                  messages=MESSAGES, max_tokens=192)
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        await protocol.run(**kwargs)
