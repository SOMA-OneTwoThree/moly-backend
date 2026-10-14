"""Fixed Korean persona evaluation: one bounded, retry-free SDK call."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from app.config import settings
from app.services import llm, model_eval, prompts

PROTOCOL_IDS = ("voice-base-ko-v5", "voice-free-ko-v5")
_TIMEOUT_S = 10.0


def protocol_profile(candidate_id: str) -> tuple[str, str, tuple[int, ...]]:
    """Return the existing fixed Korean plain evaluation profile."""
    if candidate_id not in PROTOCOL_IDS:
        raise ValueError("Unknown reply protocol")
    return "gpt-6-luna", "none", (192, 1024)


def response_format(candidate_id: str) -> None:
    protocol_profile(candidate_id)
    return None


def protocol_manifest(candidate_id: str, max_tokens: int) -> dict:
    model, reasoning, caps = protocol_profile(candidate_id)
    if max_tokens not in caps:
        raise ValueError("Reply protocol requires its registered output cap")
    fixed = {
        "model": model, "reasoning_effort": reasoning,
        "max_completion_tokens": max_tokens, "timeout": _TIMEOUT_S,
        "max_retries": 0, "response_format": None,
    }
    return {
        "response_format": None,
        "schema_sha256": hashlib.sha256(b"null").hexdigest(),
        "wire_options_sha256": hashlib.sha256(json.dumps(
            fixed, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest(),
    }


def build_messages(system: str, messages: list[dict], candidate_id: str) -> list[dict]:
    """Assemble the exact SDK message list, shared by bounds, cost reservation and sending."""
    protocol_profile(candidate_id)
    _, transcript = llm.from_openai_messages(messages)
    return llm.to_openai_messages(system, transcript)


def input_bytes(system: str, messages: list[dict], candidate_id: str) -> int:
    envelope = {
        "messages": build_messages(system, messages, candidate_id),
        "response_format": response_format(candidate_id),
    }
    return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())


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
    wire_messages = build_messages(system, messages, candidate_id)
    diagnostic = {
        "schema_sha256": manifest["schema_sha256"],
        "wire_options_sha256": manifest["wire_options_sha256"],
        "request_sha256": None,
        "sdk_create_calls": 0, "usage_known": False,
        "raw_json": None, "decisions": None, "field_order": None, "failure_kind": None,
    }
    result = model_eval.EvalResult("openai", model, None, 0, 0, 0, None)
    started = time.perf_counter()
    try:
        client = llm._get_openai_client().with_options(max_retries=0)
        kwargs = {
            "model": model, "messages": wire_messages,
            "reasoning_effort": reasoning, "max_completion_tokens": max_tokens,
            "timeout": _TIMEOUT_S,
        }
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
            if not raw.strip() or len(raw) > 600:
                raise ValueError("Invalid spoken text")
            result.text = raw  # preserve exact plain text; never strip or normalize
        except (ValueError, TypeError):
            diagnostic["failure_kind"] = "invalid_response"
            result.error = "ReplyProtocolInvalidResponse"
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    return {**result.__dict__, "prototype": diagnostic}
