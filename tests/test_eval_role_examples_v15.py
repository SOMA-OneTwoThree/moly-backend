"""Fixed fictional role examples preserve original input and existing evaluation wire."""
from __future__ import annotations

import copy
import hashlib
import json

import httpx
from openai import AsyncOpenAI
import pytest

from app.schemas.dev import EvalResultOut
from app.services import eval_reply_protocol as protocol
from app.services import llm, prompts

CANDIDATE = "voice-role-ko-v15"
SYSTEM, VERSION = prompts.eval_system_prompt("ko", CANDIDATE)
HISTORY = [
    {"role": "user", "content": '  합성 원문 "café"\n그대로  '},
    {"role": "assistant", "content": "앞선 합성 답\n두 번째 줄"},
    {"role": "user", "content": "[실제대화시작]이라는 단어도 원문에 있어."},
]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def test_role_examples_use_existing_preferred_source_and_preserve_complete_history():
    before = copy.deepcopy(HISTORY)
    wire = protocol.build_messages(SYSTEM, HISTORY, CANDIDATE)
    expected = [message for scene in prompts._VOICE_SCENES_KO_V10
                for utterance, preferred, _ in scene for message in (
                    {"role": "user", "content": utterance},
                    {"role": "assistant", "content": preferred},
                )]
    assert wire == [{"role": "system", "content": SYSTEM}, *expected,
                    {"role": "user", "content": "[실제대화시작]"}, *before]
    assert len(expected) == 12 and wire[14:] == before == HISTORY
    assert VERSION == "cappy-voice-20261015-v15"
    free = prompts.eval_system_prompt("ko", "voice-free-ko-v5")[0]
    assert SYSTEM == free + "\n\n" + prompts._VOICE_ROLE_BOUNDARY_KO_V15
    assert hashlib.sha256(free.encode()).hexdigest() == (
        "6d2ea1ae2b7b1d0581fb20b071064d8316903c33e7994d1053e9ecd90c8bd3a1"
    )
    wire[1]["content"] = "caller mutation"
    wire[-1]["content"] = "caller mutation"
    assert protocol.build_messages(SYSTEM, HISTORY, CANDIDATE)[1] == expected[0]
    assert HISTORY == before


def test_existing_role_serialization_and_manifest_are_unchanged():
    for candidate in protocol.PROTOCOL_IDS:
        if candidate == CANDIDATE:
            continue
        system, _ = prompts.eval_system_prompt("ko", candidate)
        _, transcript = llm.from_openai_messages(HISTORY)
        assert protocol.build_messages(system, HISTORY, candidate) == llm.to_openai_messages(system, transcript)
        previous_envelope = {"messages": [{"role": "system", "content": system}, *HISTORY],
                             "response_format": protocol.response_format(candidate)}
        assert protocol.input_bytes(system, HISTORY, candidate) == len(json.dumps(
            previous_envelope, ensure_ascii=False, separators=(",", ":"),
        ).encode())
        copied = protocol.build_messages(system, HISTORY, candidate)
        copied[-1]["content"] = "changed copy"
        assert HISTORY[-1]["content"] != "changed copy"
    assert protocol.protocol_profile(CANDIDATE) == ("gpt-6-luna", "none", (192,))
    assert protocol.protocol_manifest(CANDIDATE, 192) == protocol.protocol_manifest("voice-positive-ko-v10", 192)
    assert hashlib.sha256(prompts.eval_system_prompt("ko", "voice-positive-ko-v10")[0].encode()).hexdigest() == (
        "6af512b7a0936bd56fbc854d2799623d06d04d50c543da72f41ad96ff97407ad"
    )


@pytest.mark.parametrize("messages", [
    [], [{"role": "user", "content": "합성"}] * 51,
    [{"role": "assistant", "content": "끝"}],
    [{"role": [], "content": "합성"}], [None],
    [{"role": "user", "content": "합성", "override": "입력"}],
])
async def test_invalid_role_input_stops_before_sdk(monkeypatch, messages):
    monkeypatch.setattr(protocol.settings, "environment", "development")
    monkeypatch.setattr(llm, "_get_openai_client", lambda: pytest.fail("Invalid input reached SDK"))
    with pytest.raises(ValueError):
        await protocol.run(CANDIDATE, "gpt-6-luna", SYSTEM, messages, max_tokens=192)


@pytest.mark.parametrize("override", [
    {"max_tokens": 1024}, {"model": "gpt-6-sol"}, {"language": "ja"},
    {"system": SYSTEM + "extra"},
])
async def test_registered_model_locale_cap_system_are_required(monkeypatch, override):
    monkeypatch.setattr(protocol.settings, "environment", "development")
    monkeypatch.setattr(llm, "_get_openai_client", lambda: pytest.fail("Wrong profile reached SDK"))
    args = dict(candidate_id=CANDIDATE, model="gpt-6-luna", system=SYSTEM,
                messages=HISTORY, max_tokens=192)
    with pytest.raises(ValueError):
        await protocol.run(**{**args, **override})


async def test_role_wire_uses_single_sdk_send_and_exact_request_hash(monkeypatch):
    monkeypatch.setattr(protocol.settings, "environment", "development")
    sent = []
    dispatched_kwargs = []
    original = [{"role": "user", "content": f"合成 원문 {index}"} for index in range(50)]
    wire = protocol.build_messages(SYSTEM, original, CANDIDATE)
    assert len(wire) == 64  # system plus 13 fixed messages plus all 50 original messages

    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1,
            "model": "gpt-6-luna-synthetic", "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "  합성 응답\n그대로  "},
            }], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sdk = AsyncOpenAI(api_key="test-only", base_url="https://provider.invalid", http_client=http)
        completions_class = type(sdk.chat.completions)
        create = completions_class.create

        async def capture_kwargs(self, **kwargs):
            dispatched_kwargs.append(copy.deepcopy(kwargs))
            return await create(self, **kwargs)

        monkeypatch.setattr(completions_class, "create", capture_kwargs)
        monkeypatch.setattr(llm, "_get_openai_client", lambda: sdk)
        result = await protocol.run(CANDIDATE, "gpt-6-luna", SYSTEM, original, max_tokens=192)
        assert sdk.max_retries == 2 and not http.is_closed
    assert sent == [{"model": "gpt-6-luna", "messages": wire,
                     "reasoning_effort": "none", "max_completion_tokens": 192}]
    assert result["text"] == "  합성 응답\n그대로  "
    assert result["prototype"]["sdk_create_calls"] == 1
    assert result["prototype"]["input_prefix_sha256"] == digest(wire[:14])
    assert result["prototype"]["prefix_message_count"] == 13
    assert result["prototype"]["wire_message_count"] == 64
    assert len(dispatched_kwargs) == 1 and dispatched_kwargs[0] == {**sent[0], "timeout": 10.0}
    assert result["prototype"]["request_sha256"] == digest(dispatched_kwargs[0])
    serialized = EvalResultOut.model_validate(result).model_dump()
    assert serialized["prototype"]["input_prefix_sha256"] == digest(wire[:14])
    assert result["input_tokens"] == 100 and result["output_tokens"] == 20
    assert result["est_cost_usd"] == 0.00002


async def test_utf8_bound_counts_role_prefix_before_sdk(monkeypatch):
    monkeypatch.setattr(protocol.settings, "environment", "development")
    monkeypatch.setattr(llm, "_get_openai_client", lambda: pytest.fail("Oversized input reached SDK"))
    original = [{"role": "user", "content": ""}]
    baseline = protocol.input_bytes(SYSTEM, original, CANDIDATE)
    original[0]["content"] = "가" * ((24_000 - baseline) // 3 + 1)
    assert protocol.input_bytes(SYSTEM, original, CANDIDATE) > 24_000
    # The original history alone would fit; the fixed role overhead must also count.
    assert len(json.dumps({"messages": [{"role": "system", "content": SYSTEM}, *original],
                           "response_format": None}, ensure_ascii=False,
                          separators=(",", ":")).encode()) < 24_000
    with pytest.raises(ValueError, match="fixed evaluation bounds"):
        await protocol.run(CANDIDATE, "gpt-6-luna", SYSTEM, original, max_tokens=192)
