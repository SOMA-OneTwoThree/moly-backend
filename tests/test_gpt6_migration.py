"""Model migration: actual cost buckets never change user quota; old SDK/rollback compatible."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletion

from app.config import Settings
from app.services import llm, model_eval, usage_ledger


def response(write="missing"):
    details = {"cached_tokens": 3000}
    if write != "missing":
        details["cache_write_tokens"] = write
    return ChatCompletion.model_validate({
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
        "model": "gpt-6-luna", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "괜찮아"}}],
        "usage": {"prompt_tokens": 5000, "completion_tokens": 100, "total_tokens": 5100,
                  "prompt_tokens_details": details},
    })


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-6-luna", "gpt-6-sol"])
@pytest.mark.parametrize("write,plain,cost_write,estimated", [
    (0, 2000, 0, False), (800, 1200, 800, False),
    ("missing", 0, 2000, True), (None, 0, 2000, True),
])
async def test_cost_usage_and_quota_are_independent(monkeypatch, model, write, plain, cost_write, estimated):
    raw = response(write)
    create = AsyncMock(return_value=raw)
    monkeypatch.setattr(llm, "_get_openai_client", lambda: NS(chat=NS(completions=NS(create=create))))
    closed = AsyncMock()
    monkeypatch.setattr(usage_ledger, "open_call", AsyncMock(return_value=None))
    monkeypatch.setattr(usage_ledger, "close_call", closed)
    r = await llm.generate("system", [], model=model, reasoning_effort="none",
                           ledger=usage_ledger.LedgerContext(lane="foreground", purpose="chat"))
    cost = closed.call_args.kwargs
    assert (cost["input_tokens"], cost["cached_input_tokens"], cost["cache_write_tokens"]) == (plain, 3000, cost_write)
    assert cost["cache_write_estimated"] is estimated
    assert llm.billable_tokens(r) == 3400  # 3000*.1 + 2000*1.25 + 100*6
    assert llm._step_usage(raw.usage, model, "tool_final").billable == 3400
    assert create.call_args.kwargs["reasoning_effort"] == "none"


async def test_tool_step_uses_reported_cost_not_quota_estimate(monkeypatch):
    create = AsyncMock(return_value=response(800))
    monkeypatch.setattr(llm, "_get_openai_client", lambda: NS(chat=NS(completions=NS(create=create))))
    closed = AsyncMock()
    monkeypatch.setattr(usage_ledger, "close_call", closed)
    result = await llm.generate_step("s", [llm.UserText("hi")], tools=[], tool_choice="none",
        model="gpt-6-luna", max_tokens=1024, timeout=10)
    assert closed.call_args.kwargs["input_tokens"] == 1200
    assert closed.call_args.kwargs["cache_write_tokens"] == 800
    assert closed.call_args.kwargs["cache_write_estimated"] is False
    assert result.usage.billable == 3400
    assert create.call_args.kwargs["reasoning_effort"] == "none"


def test_memory_model_without_write_price_is_not_unknown_cost():
    r = llm._openai_usage(response().usage, "gpt-4.1-mini-2025-04-14")
    assert r.ledger_usage() == {"input_tokens": 2000, "cached_input_tokens": 3000,
        "cache_write_tokens": 0, "output_tokens": 100, "cache_write_estimated": False}


@pytest.mark.parametrize("prompt,mult", [(272000, 1), (272001, 2)])
def test_long_context_standard_price_boundary(prompt, mult):
    p = usage_ledger.PriceRow(20260923, "openai", "gpt-6-luna", 100000, 10000, 125000, 500000, None)
    actual = usage_ledger.compute_cost(p, input_tokens=prompt, output_tokens=100)
    from math import ceil
    expected = ceil(prompt * .1 * mult + 50 * (1 if mult == 1 else 1.5))
    assert actual == expected


async def test_eval_uses_production_usage_and_cache_rates(monkeypatch):
    monkeypatch.setattr(model_eval, "_ADAPTERS", {"openai": AsyncMock(return_value=llm._openai_usage(response(800).usage, "gpt-6-luna"))})
    r = await model_eval.run_eval("openai", "gpt-6-luna", "s", [])
    assert r.est_cost_usd == pytest.approx((1200*.1 + 3000*.01 + 800*.125 + 100*.5)/1e6)
    assert r.input_tokens == 5000
    assert r.cache_write_tokens == 800 and not r.cache_write_estimated


def test_migration_defaults_and_explicit_rollback(monkeypatch):
    for key in ("MODEL_CHAT", "MODEL_DIARY", "MODEL_UTILITY"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)
    assert (s.model_chat, s.model_diary, s.model_utility) == ("gpt-6-luna", "gpt-6-sol", "gpt-6-luna")
    monkeypatch.setenv("MODEL_CHAT", "gpt-5.6-luna")
    monkeypatch.setenv("MODEL_DIARY", "gpt-5.6-terra")
    monkeypatch.setenv("MODEL_UTILITY", "gpt-5.6-luna")
    s = Settings(_env_file=None)
    assert (s.model_chat, s.model_diary, s.model_utility) == ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-luna")


async def test_diary_verdict_has_output_headroom_and_no_reasoning(monkeypatch):
    from app.services import diary_generation

    generate = AsyncMock(return_value=llm.LLMResult("OK", 10, 4))
    monkeypatch.setattr(llm, "generate", generate)
    assert await diary_generation._self_check("A presentation ended.", "I finished my presentation.", language="en")
    assert generate.call_args.kwargs["reasoning_effort"] == "none"
    assert generate.call_args.kwargs["max_tokens"] == 64
