"""Fixed v5 plain evaluation keeps one SDK send, exact text and honest usage."""

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

BASE, FREE = "voice-base-ko-v5", "voice-free-ko-v5"
CONVERSATION = "voice-conversation-ko-v6"
STRUCTURED = "voice-structured-ko-v7"
EXAMPLES = "voice-structured-ko-v8"
ORDER_ONLY = "voice-structured-ko-v8-order"
EXAMPLES_ONLY = "voice-structured-ko-v8-examples"
MESSAGES = [{"role": "user", "content": '  합성 "café"\n그대로  '}]


def completion(text, *, finish="stop", refusal=None, usage=True):
    return SimpleNamespace(
        model="gpt-6-luna-synthetic",
        choices=[
            SimpleNamespace(
                finish_reason=finish,
                message=SimpleNamespace(
                    content=text,
                    refusal=refusal,
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=None)
        if usage
        else None,
    )


class Client:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []
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


async def call(monkeypatch, response, candidate=FREE):
    client = Client(response)
    monkeypatch.setattr(llm, "_get_openai_client", lambda: client)
    system = prompts.eval_system_prompt("ko", candidate)[0]
    result = await protocol.run(candidate, "gpt-6-luna", system, MESSAGES, max_tokens=192)
    assert len(client.calls) == result["prototype"]["sdk_create_calls"] == 1
    return result, client


@pytest.mark.parametrize("candidate", (BASE, FREE))
async def test_v5_plain_wire_and_diagnostics_preserve_registered_contract(monkeypatch, candidate):
    system, version = prompts.eval_system_prompt("ko", candidate)
    result, client = await call(monkeypatch, completion("  합성 답\n그대로  "), candidate)
    assert version == "cappy-voice-20261014-v5"
    assert client.calls == [
        {
            "model": "gpt-6-luna",
            "messages": [{"role": "system", "content": system}, *MESSAGES],
            "reasoning_effort": "none",
            "max_completion_tokens": 192,
            "timeout": 10.0,
        }
    ]
    assert result["text"] == "  합성 답\n그대로  "
    assert result["input_tokens"] == 100 and result["output_tokens"] == 20
    assert result["est_cost_usd"] == 0.00002
    assert result["model_snapshot"] == "gpt-6-luna-synthetic"
    assert set(result["prototype"]) == {
        "schema_sha256",
        "wire_options_sha256",
        "request_sha256",
        "sdk_create_calls",
        "usage_known",
        "raw_json",
        "decisions",
        "field_order",
        "failure_kind",
    }
    assert result["prototype"]["raw_json"] == result["text"]
    assert result["prototype"]["decisions"] is result["prototype"]["field_order"] is None
    assert (
        result["prototype"]["request_sha256"]
        == hashlib.sha256(
            json.dumps(
                client.calls[0],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    assert protocol.protocol_profile(candidate) == ("gpt-6-luna", "none", (192, 1024))
    assert protocol.protocol_manifest(candidate, 192)["wire_options_sha256"] == (
        "447d100fb620c39acb6f0654237349590503724fa0e672d77671fd8f419056eb"
    )
    assert protocol.protocol_manifest(candidate, 1024)["wire_options_sha256"] == (
        "3d5d5eda230c5f1d2379e97ddc3dd67c17ed3554d35b757f33cb4a7f61505ea6"
    )
    assert protocol.response_format(candidate) is None


@pytest.mark.parametrize("candidate", (
    CONVERSATION, STRUCTURED, EXAMPLES, ORDER_ONLY, EXAMPLES_ONLY,
))
async def test_korean_candidates_keep_existing_model_and_one_192_token_send(monkeypatch, candidate):
    result, client = await call(monkeypatch, completion("합성 답"), candidate)
    system = prompts.eval_system_prompt("ko", candidate)[0]
    assert client.calls == [{
        "model": "gpt-6-luna", "messages": [{"role": "system", "content": system}, *MESSAGES],
        "reasoning_effort": "none", "max_completion_tokens": 192, "timeout": 10.0,
    }]
    assert result["text"] == "합성 답"
    assert protocol.protocol_profile(candidate) == ("gpt-6-luna", "none", (192,))
    assert protocol.protocol_manifest(candidate, 192) == protocol.protocol_manifest(FREE, 192)


@pytest.mark.parametrize("overrides", (
    {"max_tokens": 1024}, {"model": "gpt-6-sol"}, {"language": "ja"}, {"system": "override"},
))
@pytest.mark.parametrize("candidate", (
    CONVERSATION, STRUCTURED, EXAMPLES, ORDER_ONLY, EXAMPLES_ONLY,
))
async def test_korean_candidates_reject_changed_profile_before_sdk(monkeypatch, overrides, candidate):
    monkeypatch.setattr(llm, "_get_openai_client", lambda: pytest.fail("Invalid input reached SDK"))
    args = dict(
        candidate_id=candidate, model="gpt-6-luna", max_tokens=192,
        system=prompts.eval_system_prompt("ko", candidate)[0], messages=MESSAGES,
    )
    with pytest.raises(ValueError):
        await protocol.run(**{**args, **overrides})


@pytest.mark.parametrize(
    "finish,refusal,text",
    [
        ("length", None, "답"),
        ("tool_calls", None, "답"),
        ("", None, "답"),
        ("stop", "refused", "답"),
        ("stop", None, None),
        ("stop", None, "  "),
        ("stop", None, "가" * 601),
    ],
)
async def test_incomplete_response_preserves_known_cost_without_second_call(
    monkeypatch, finish, refusal, text
):
    result, _ = await call(monkeypatch, completion(text, finish=finish, refusal=refusal))
    assert result["text"] is None and result["error"] == "ReplyProtocolInvalidResponse"
    assert result["finish_reason"] == finish
    assert result["est_cost_usd"] == 0.00002 and result["output_tokens"] == 20


async def test_missing_usage_never_means_free_call(monkeypatch):
    result, _ = await call(monkeypatch, completion("합성 답", usage=False))
    assert result["text"] == "합성 답"
    assert result["est_cost_usd"] is None and result["prototype"]["usage_known"] is False


async def test_initialization_failure_counts_no_send_and_hides_provider_detail(monkeypatch, caplog):
    def fail():
        raise RuntimeError("synthetic private detail")

    monkeypatch.setattr(llm, "_get_openai_client", fail)
    result = await protocol.run(
        FREE, "gpt-6-luna", prompts.eval_system_prompt("ko", FREE)[0], MESSAGES, max_tokens=192
    )
    assert result["prototype"]["sdk_create_calls"] == 0 and result["est_cost_usd"] is None
    assert "synthetic private detail" not in caplog.text + json.dumps(result)


async def test_external_cancellation_propagates(monkeypatch):
    client = Client(error=asyncio.CancelledError())
    monkeypatch.setattr(llm, "_get_openai_client", lambda: client)
    with pytest.raises(asyncio.CancelledError):
        await protocol.run(
            FREE, "gpt-6-luna", prompts.eval_system_prompt("ko", FREE)[0], MESSAGES, max_tokens=192
        )
    assert len(client.calls) == 1


@pytest.mark.parametrize("status", (429, 500, "read_timeout"))
async def test_installed_sdk_sends_once_and_keeps_shared_client_open(monkeypatch, status):
    sent = []

    async def handler(request):
        sent.append(request)
        if status == "read_timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(
            status, json={"error": {"message": "synthetic", "type": "server_error"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sdk = AsyncOpenAI(
            api_key="test-only", base_url="https://provider.invalid", http_client=http
        )
        monkeypatch.setattr(llm, "_get_openai_client", lambda: sdk)
        result = await protocol.run(
            FREE, "gpt-6-luna", prompts.eval_system_prompt("ko", FREE)[0], MESSAGES, max_tokens=192
        )
        assert len(sent) == result["prototype"]["sdk_create_calls"] == 1
        assert result["text"] is result["est_cost_usd"] is None
        assert sdk.max_retries == 2 and not http.is_closed


@pytest.mark.parametrize(
    "override",
    [
        {"language": "ja"},
        {"model": "gpt-6-sol"},
        {"max_tokens": 193},
        {"system": "arbitrary"},
        {"candidate_id": "voice-role-ko-v15"},
    ],
)
async def test_unregistered_settings_and_removed_ids_never_reach_sdk(monkeypatch, override):
    monkeypatch.setattr(llm, "_get_openai_client", lambda: pytest.fail("Invalid input reached SDK"))
    args = dict(
        candidate_id=FREE,
        model="gpt-6-luna",
        max_tokens=192,
        system=prompts.eval_system_prompt("ko", FREE)[0],
        messages=MESSAGES,
    )
    with pytest.raises(ValueError):
        await protocol.run(**{**args, **override})


@pytest.mark.parametrize("candidate", protocol.PROTOCOL_IDS)
def test_full_history_serialization_and_utf8_bounds_keep_no_role_prefix(candidate):
    system = prompts.eval_system_prompt("ko", candidate)[0]
    wire = protocol.build_messages(system, MESSAGES, candidate)
    assert wire == [{"role": "system", "content": system}, *MESSAGES]
    expected = {"messages": wire, "response_format": None}
    assert protocol.input_bytes(system, MESSAGES, candidate) == len(
        json.dumps(
            expected,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    wire[-1]["content"] = "changed copy"
    assert MESSAGES[-1]["content"] != "changed copy"
