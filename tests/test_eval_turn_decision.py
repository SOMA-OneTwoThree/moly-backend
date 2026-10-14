"""Synthetic role/source/deadline contracts; no quality or production activation claim."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI
import pytest

from app.services import eval_reply_protocol as protocol, eval_turn_decision as turn, llm, prompts
from tests.test_eval_reply_protocol import completion

MESSAGES = [{"role": "user", "content": '원문 “파랑”\n原文'},
            {"role": "assistant", "content": "파랑을 골랐어?"},
            {"role": "user", "content": "아니, 지금은 주황이야."}]
DECISION = {"current_request": "constraint_update", "known_user_turn_indices": [0],
            "question_target": None, "reply_goal": "정정된 주황색에 답하기"}


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture(autouse=True)
def development(monkeypatch):
    monkeypatch.setattr(turn.settings, "environment", "development")


async def run(monkeypatch, responses, *, messages=MESSAGES, **overrides):
    client = Client(responses)
    monkeypatch.setattr(llm, "_get_openai_client", lambda: client)
    args = dict(candidate_id=turn.CANDIDATE_ID, model=turn.MODEL,
                system=prompts.eval_system_prompt("ko", turn.CANDIDATE_ID)[0],
                messages=messages, max_tokens=192)
    result = await protocol.run(**{**args, **overrides})
    return result, client


def plan_response(decision=DECISION, **kwargs):
    return completion(json.dumps(decision, ensure_ascii=False), **kwargs)


@pytest.mark.parametrize("indices", [[1], [True], [-1], [3], [0, 0], [0, 2, 0, 2], ["0"]])
def test_selected_indices_are_unique_original_user_only_and_bounded(indices):
    with pytest.raises(ValueError):
        turn.parse_decision(json.dumps({**DECISION, "known_user_turn_indices": indices}), MESSAGES)


@pytest.mark.parametrize("overrides", [
    {"current_request": "other"}, {"question_target": False}, {"question_target": "가" * 101},
    {"reply_goal": " "}, {"reply_goal": "가" * 101}, {"extra": "unused"},
])
def test_decision_fields_have_no_extra_analysis_or_unbounded_text(overrides):
    with pytest.raises(ValueError):
        turn.parse_decision(json.dumps({**DECISION, **overrides}), MESSAGES)


def test_duplicate_keys_are_rejected_and_schema_has_only_supported_wire_constraints():
    raw = json.dumps(DECISION)[:-1] + ', "reply_goal":"second"}'
    with pytest.raises(ValueError):
        turn.parse_decision(raw, MESSAGES)
    schema = turn.PLAN_FORMAT["json_schema"]["schema"]
    assert list(schema["properties"]) == schema["required"] == list(DECISION)
    assert schema["additionalProperties"] is False
    assert "uniqueItems" not in json.dumps(schema) and "maxLength" not in json.dumps(schema)


async def test_two_stage_preserves_original_order_current_user_and_server_resolved_source(monkeypatch):
    original = copy.deepcopy(MESSAGES)
    result, client = await run(monkeypatch, [plan_response(), completion('  응, 주황.\n  ')])
    assert MESSAGES == original and len(client.calls) == 2
    planner_data = json.loads(client.calls[0]["messages"][1]["content"])
    assert planner_data == {"original_conversation": [{"index": i, **m} for i, m in enumerate(original)]}
    rendered = client.calls[1]["messages"][1:]
    assert rendered[:-2] == original[:-1] and rendered[-1] == original[-1]
    metadata = rendered[-2]
    assert metadata["role"] == "user"
    assert json.loads(metadata["content"])["original_user_turns"] == [{"index": 0, **original[0]}]
    assert json.loads(metadata["content"])["decision"] == DECISION
    assert result["text"] == '  응, 주황.\n  '
    assert result["prototype"]["sdk_create_calls"] == 2
    assert result["prototype"]["parsed_decision"] == DECISION
    assert result["est_cost_usd"] == 0.00004 and result["output_tokens"] == 40
    for record, kwargs in zip(result["prototype"]["subcalls"], client.calls):
        assert record["request_sha256"] == hashlib.sha256(json.dumps(
            kwargs, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        assert record["sdk_create_calls"] == 1 and record["usage_known"] is True
    assert client.calls[0]["reasoning_effort"] == "low" and client.calls[0]["timeout"] <= 4
    assert client.calls[1]["reasoning_effort"] == "none" and client.calls[1]["timeout"] <= 5


@pytest.mark.parametrize("response", [
    completion("{}"), plan_response(finish="length"), plan_response(refusal="refused"),
    completion(" "), completion(json.dumps({**DECISION, "known_user_turn_indices": [1]})),
])
async def test_invalid_or_incomplete_plan_never_starts_renderer_and_preserves_cost(monkeypatch, response):
    result, client = await run(monkeypatch, [response])
    assert len(client.calls) == 1 and result["text"] is None
    assert result["prototype"]["sdk_create_calls"] == 1
    assert result["est_cost_usd"] == result["prototype"]["known_cost_subtotal_usd"] == 0.00002


async def test_missing_plan_usage_stops_before_render_even_for_valid_decision(monkeypatch):
    result, client = await run(monkeypatch, [plan_response(usage=False)])
    assert len(client.calls) == 1 and result["text"] is None
    assert result["est_cost_usd"] is None and result["prototype"]["usage_known"] is False
    assert result["prototype"]["failure_kind"] == "unknown_plan_usage"


async def test_partial_renderer_usage_keeps_known_subtotal_and_unknown_total(monkeypatch):
    result, client = await run(monkeypatch, [plan_response(), completion("응.", usage=False)])
    assert len(client.calls) == 2 and result["text"] == "응."
    assert result["est_cost_usd"] is None
    assert result["prototype"]["known_cost_subtotal_usd"] == 0.00002
    assert result["prototype"]["usage_known"] is False


async def test_renderer_wire_bound_does_not_trim_history_or_start_another_call(monkeypatch):
    messages = [{"role": "user", "content": "가" * 4000}, {"role": "user", "content": "계속"}]
    result, client = await run(monkeypatch, [plan_response()], messages=messages)
    assert len(client.calls) == 1 and result["text"] is None
    assert result["prototype"]["subcalls"][-1]["status"] == "input_bound"
    assert result["prototype"]["subcalls"][-1]["sdk_create_calls"] == 0
    assert result["prototype"]["usage_known"] is True and result["est_cost_usd"] == 0.00002


@pytest.mark.parametrize("overrides", [
    {"model": "gpt-6-sol"}, {"max_tokens": 1024}, {"language": "ja"}, {"system": "arbitrary"},
    {"messages": [{"role": [], "content": "synthetic"}]},
    {"messages": [{"role": "system", "content": "synthetic"}]},
])
async def test_invalid_input_has_zero_sdk_calls_and_known_zero_spend(monkeypatch, overrides):
    result, client = await run(monkeypatch, [], **overrides)
    assert client.calls == [] and result["error"] == "TurnDecisionEvaluationFailed"
    assert result["prototype"]["sdk_create_calls"] == 0
    assert result["prototype"]["usage_known"] is True and result["est_cost_usd"] == 0


async def test_cpu_work_consuming_whole_deadline_prevents_renderer_dispatch(monkeypatch):
    original_parse = turn.parse_decision

    def delayed_parse(raw, messages):
        time.sleep(0.03)
        return original_parse(raw, messages)

    monkeypatch.setattr(turn, "TOTAL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(turn, "parse_decision", delayed_parse)
    result, client = await run(monkeypatch, [plan_response()])
    assert len(client.calls) == 1 and result["text"] is None
    assert result["prototype"]["subcalls"][-1]["status"] == "deadline"


async def test_external_cancellation_propagates_with_no_fallback(monkeypatch):
    with pytest.raises(asyncio.CancelledError):
        await run(monkeypatch, [asyncio.CancelledError()])


async def test_installed_sdk_uses_retry_free_clone_and_never_closes_shared_client(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        raw = json.dumps(DECISION) if len(requests) == 1 else "응, 주황."
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1,
            "model": "gpt-6-luna-synthetic", "choices": [{
                "index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": raw},
            }], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                          "completion_tokens_details": {"reasoning_tokens": 7}},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sdk = AsyncOpenAI(api_key="test-only", base_url="https://provider.invalid", http_client=http)
        monkeypatch.setattr(llm, "_get_openai_client", lambda: sdk)
        result = await turn.run(turn.CANDIDATE_ID, turn.MODEL,
                                prompts.eval_system_prompt("ko", turn.CANDIDATE_ID)[0],
                                MESSAGES, max_tokens=192)
        assert sdk.max_retries == 2 and not http.is_closed
    assert len(requests) == 2 and result["est_cost_usd"] == 0.00004
    assert result["output_tokens"] == 40
    assert [record["reasoning_tokens"] for record in result["prototype"]["subcalls"]] == [7, 7]
    assert requests[0]["response_format"] == turn.PLAN_FORMAT
    assert "response_format" not in requests[1]


@pytest.mark.parametrize("status", [400, 429, 500, "read_timeout"])
async def test_installed_sdk_failed_plan_is_one_send_without_renderer_or_retry(monkeypatch, status):
    requests = []

    async def handler(request):
        requests.append(request)
        if status == "read_timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        return httpx.Response(status, json={"error": {"message": "synthetic", "type": "server_error"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sdk = AsyncOpenAI(api_key="test-only", base_url="https://provider.invalid", http_client=http)
        monkeypatch.setattr(llm, "_get_openai_client", lambda: sdk)
        result = await turn.run(turn.CANDIDATE_ID, turn.MODEL,
                                prompts.eval_system_prompt("ko", turn.CANDIDATE_ID)[0],
                                MESSAGES, max_tokens=192)
        assert sdk.max_retries == 2 and not http.is_closed
    assert len(requests) == result["prototype"]["sdk_create_calls"] == 1
    assert result["text"] is None and result["est_cost_usd"] is None


async def test_v12_low_condition_keeps_exact_control_system_and_single_call(monkeypatch):
    identifier = protocol.V12_PROTOCOL_IDS[0]
    control = prompts.eval_system_prompt("ko", "voice-control-ko-v11")[0]
    assert prompts.eval_system_prompt("ko", identifier) == (control, "cappy-voice-20261015-v12")
    client = Client([completion("응.")])
    monkeypatch.setattr(llm, "_get_openai_client", lambda: client)
    result = await protocol.run(identifier, turn.MODEL, control, MESSAGES, max_tokens=1024)
    assert len(client.calls) == 1 and result["text"] == "응."
    assert client.calls[0]["reasoning_effort"] == "low" and client.calls[0]["timeout"] == 10
