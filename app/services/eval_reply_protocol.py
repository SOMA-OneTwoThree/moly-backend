"""One-call reply-format experiment; authorized development evaluation only."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from app.config import settings
from app.services import llm, model_eval, prompts

V4_PROTOCOL_IDS = ("voice-plain-ko-v4", "voice-speech-ko-v4", "voice-act-focus-ko-v4")
_V6_PROFILES = {
    "voice-luna-none-ko-v6": ("gpt-6-luna", "none", (1024,)),
    "voice-luna-low-ko-v6": ("gpt-6-luna", "low", (1024,)),
    "voice-sol-none-ko-v6": ("gpt-6-sol", "none", (1024,)),
}
V6_PROTOCOL_IDS = tuple(_V6_PROFILES)
V7_PROTOCOL_IDS = ("voice-base-ko-v7", "voice-example-ko-v7")
V8_PROTOCOL_IDS = ("voice-base-ko-v8", "voice-direct-ko-v8")
PLAIN_PROTOCOL_IDS = (
    V4_PROTOCOL_IDS[0], "voice-base-ko-v5", "voice-free-ko-v5",
    *V6_PROTOCOL_IDS, *V7_PROTOCOL_IDS, *V8_PROTOCOL_IDS,
)
PROTOCOL_IDS = (*V4_PROTOCOL_IDS, *PLAIN_PROTOCOL_IDS[1:])
_TIMEOUT_S = 10.0
_ACTIONS = ("reply", "choice", "opinion", "question", "listen", "end")


def protocol_profile(candidate_id: str) -> tuple[str, str, tuple[int, ...]]:
    """Return server-owned model, reasoning and caps; no caller-supplied wire options."""
    if candidate_id not in PROTOCOL_IDS:
        raise ValueError("Unknown reply protocol")
    if candidate_id in (*V7_PROTOCOL_IDS, *V8_PROTOCOL_IDS):
        return "gpt-6-luna", "none", (192,)
    return _V6_PROFILES.get(candidate_id, ("gpt-6-luna", "none", (192, 1024)))


def response_format(candidate_id: str) -> dict | None:
    if candidate_id not in PROTOCOL_IDS:
        raise ValueError("Unknown reply protocol")
    if candidate_id in PLAIN_PROTOCOL_IDS:
        return None
    properties = {}
    if candidate_id == PROTOCOL_IDS[2]:
        properties.update({
            "action": {
                "type": "string", "enum": list(_ACTIONS),
                "description": "이번 답의 주요 행동. 경청에도 필요한 확인 질문이 포함될 수 있다.",
            },
            "focus": {
                "type": "string",
                "description": "이번 답이 직접 다룰 대상이나 기여를 한 구절로. 이유나 추론 과정은 쓰지 않는다.",
            },
        })
    properties["text"] = {
        "type": "string", "description": "상대에게 실제로 건넬 캐피의 한국어 말만.",
    }
    return {"type": "json_schema", "json_schema": {
        "name": "cappy_eval_reply", "strict": True,
        "schema": {"type": "object", "properties": properties,
                   "required": list(properties), "additionalProperties": False},
    }}


def protocol_manifest(candidate_id: str, max_tokens: int) -> dict:
    model, reasoning, caps = protocol_profile(candidate_id)
    if max_tokens not in caps:
        raise ValueError("Reply protocol requires its registered output cap")
    wire_format = response_format(candidate_id)
    ordered = json.dumps(wire_format, ensure_ascii=False, separators=(",", ":"))
    fixed = {
        "model": model, "reasoning_effort": reasoning,
        "max_completion_tokens": max_tokens, "timeout": _TIMEOUT_S,
        "max_retries": 0, "response_format": wire_format,
    }
    return {
        "response_format": wire_format,
        "schema_sha256": hashlib.sha256(ordered.encode()).hexdigest(),
        "wire_options_sha256": hashlib.sha256(json.dumps(
            fixed, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest(),
    }


def input_bytes(system: str, messages: list[dict], candidate_id: str) -> int:
    envelope = {
        "messages": [{"role": "system", "content": system}, *messages],
        "response_format": response_format(candidate_id),
    }
    return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _decode(raw: str, candidate_id: str) -> dict:
    if candidate_id in PLAIN_PROTOCOL_IDS:
        values = {"text": raw}
    else:
        values = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        expected = {"text"} if candidate_id == PROTOCOL_IDS[1] else {"action", "focus", "text"}
        if type(values) is not dict or set(values) != expected:
            raise ValueError("Reply fields do not match schema")
    text = values["text"]
    if type(text) is not str or not text.strip() or len(text) > 600:
        raise ValueError("Invalid spoken text")
    if candidate_id == PROTOCOL_IDS[2]:
        if type(values["action"]) is not str or values["action"] not in _ACTIONS:
            raise ValueError("Invalid action")
        focus = values["focus"]
        if type(focus) is not str or not focus.strip() or len(focus) > 60:
            raise ValueError("Invalid focus")
    return values


async def run(
    candidate_id: str, model: str, system: str, messages: list[dict], *, max_tokens: int,
    language: str = "ko",
) -> dict:
    expected_model, reasoning, caps = protocol_profile(candidate_id)
    if settings.environment != "development" or model != expected_model or language != "ko":
        raise ValueError("Reply protocol is development-only for the registered model")
    expected, _ = prompts.eval_system_prompt(language, candidate_id)
    if system != expected:
        raise ValueError("Reply protocol requires the frozen registered system")
    if not messages or len(messages) > 50 or messages[-1].get("role") != "user":
        raise ValueError("Reply protocol requires bounded conversation input")
    if any(set(message) != {"role", "content"} or message["role"] not in {"user", "assistant"}
           or type(message["content"]) is not str for message in messages):
        raise ValueError("Reply protocol accepts only user and assistant text")
    if max_tokens not in caps or input_bytes(system, messages, candidate_id) > 24_000:
        raise ValueError("Reply protocol exceeds fixed evaluation bounds")
    manifest = protocol_manifest(candidate_id, max_tokens)
    diagnostic = {
        "schema_sha256": manifest["schema_sha256"],
        "wire_options_sha256": manifest["wire_options_sha256"],
        "request_sha256": None,
        "sdk_create_calls": 0, "usage_known": False,
        "raw_json": None, "decisions": None, "field_order": None, "failure_kind": None,
    }
    if candidate_id in V6_PROTOCOL_IDS:
        diagnostic["reasoning_tokens"] = None
    result = model_eval.EvalResult("openai", model, None, 0, 0, 0, None)
    started = time.perf_counter()
    try:
        client = llm._get_openai_client().with_options(max_retries=0)
        _, transcript = llm.from_openai_messages(messages)
        kwargs = {
            "model": model, "messages": llm.to_openai_messages(system, transcript),
            "reasoning_effort": reasoning, "max_completion_tokens": max_tokens,
            "timeout": _TIMEOUT_S,
        }
        if manifest["response_format"] is not None:
            kwargs["response_format"] = manifest["response_format"]
        diagnostic["request_sha256"] = hashlib.sha256(json.dumps(
            kwargs, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        async with asyncio.timeout(_TIMEOUT_S):
            diagnostic["sdk_create_calls"] = 1
            response = await client.chat.completions.create(**kwargs)
    except Exception as error:
        diagnostic["failure_kind"] = "provider_unavailable"
        result.error = type(error).__name__  # no provider detail, response or input in logs
    else:
        result.model_snapshot = getattr(response, "model", None) or ""
        usage = getattr(response, "usage", None)
        if candidate_id in V6_PROTOCOL_IDS:
            details = getattr(usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", None)
            if type(reasoning_tokens) is int and reasoning_tokens >= 0:
                diagnostic["reasoning_tokens"] = reasoning_tokens
        known = all(type(getattr(usage, field, None)) is int and getattr(usage, field) >= 0
                    for field in ("prompt_tokens", "completion_tokens"))
        diagnostic["usage_known"] = known
        if known:
            counted = llm._openai_usage(usage, model)
            buckets = counted.ledger_usage()
            plain, cached, write = (buckets[key] for key in (
                "input_tokens", "cached_input_tokens", "cache_write_tokens",
            ))
            result.input_tokens = plain + cached + write
            result.output_tokens = counted.output_tokens
            result.cached_input_tokens = cached
            result.cache_write_tokens = write
            result.cache_write_estimated = counted.cache_write_estimated
            result.est_cost_usd = model_eval._cost(
                model, plain, counted.output_tokens, cached=cached, write=write,
            )
        choices = getattr(response, "choices", None) or []
        choice = choices[0] if len(choices) == 1 else None
        result.finish_reason = getattr(choice, "finish_reason", None) or ""
        message = getattr(choice, "message", None)
        raw = getattr(message, "content", None)
        diagnostic["raw_json"] = raw if type(raw) is str else None
        try:
            if choice is None or result.finish_reason != "stop" or getattr(message, "refusal", None):
                raise ValueError("Incomplete or refused response")
            if type(raw) is not str:
                raise ValueError("Missing response content")
            values = _decode(raw, candidate_id)
            diagnostic["field_order"] = list(values) if candidate_id not in PLAIN_PROTOCOL_IDS else None
            result.text = values["text"]  # exact decoded text: never strip or normalize
            if candidate_id == PROTOCOL_IDS[2]:
                diagnostic["decisions"] = {key: values[key] for key in ("action", "focus")}
        except (ValueError, TypeError):
            diagnostic["failure_kind"] = "invalid_response"
            result.error = "ReplyProtocolInvalidResponse"
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    return {**result.__dict__, "prototype": diagnostic}
