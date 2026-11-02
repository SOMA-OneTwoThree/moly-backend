"""병렬 준비와 단일 발행의 경계를 검증한다. 실제 모델·DB는 사용하지 않는다."""
import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest

from app.models.message import Message
from app.services import chat, crisis_chat, crisis_guard, llm, prompts
from app.services import usage_ledger
from tests.test_chat import FakeSession, _gating


def response(text="ordinary", **kw):
    return llm.LLMResult(text, 10, 2, model="gpt-6-luna", finish_reason=kw.get("finish", "stop"))


@pytest.mark.parametrize(("route", "expected"), [("normal", "ordinary"), ("safety", None)])
async def test_draft_and_decision_start_together_but_only_selected_reply_survives(route, expected):
    draft_started = asyncio.Event()
    decision_started = asyncio.Event()
    tools = []

    async def decide():
        decision_started.set()
        await draft_started.wait()
        return crisis_guard.DecisionResult("ok", route=route)

    async def draft(gate):
        draft_started.set()
        await decision_started.wait()
        if await gate():
            tools.append("executed")
        return response(), None

    async def no_reask():
        raise AssertionError("reask must not run")

    result = await crisis_chat.generate(
        decision=decide(), draft=draft, reask=no_reask, language="en", nickname=None,
        deadline=time.monotonic() + 1,
    )
    assert result.result.text == (expected or crisis_guard.fixed_reply("en"))
    assert tools == (["executed"] if route == "normal" else [])


async def test_incomplete_reask_uses_completed_server_fallback():
    async def decide():
        return crisis_guard.DecisionResult("ok", route="ask", method_request=False)

    async def draft(gate):
        return response(), None

    async def reask():
        return response("What happened?", finish="length")

    result = await crisis_chat.generate(
        decision=decide(), draft=draft, reask=reask, language="en", nickname=None,
        deadline=time.monotonic() + 1,
    )
    assert result.result.text == "What happened?"
    assert result.result.finish_reason == "stop"


async def test_speculative_failure_does_not_prevent_fixed_reply():
    async def decide():
        await asyncio.sleep(0)
        return crisis_guard.DecisionResult("ok", route="safety")

    async def draft(gate):
        raise TimeoutError("simulated draft failure")

    async def reask():
        raise AssertionError

    result = await crisis_chat.generate(
        decision=decide(), draft=draft, reask=reask, language="ko", nickname=None,
        deadline=time.monotonic() + 1,
    )
    assert result.result.text == crisis_guard.fixed_reply("ko")


async def test_request_cancellation_stops_both_unfinished_tasks():
    cancelled = []

    async def decide():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append("decision")

    async def draft(gate):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append("draft")

    task = asyncio.create_task(crisis_chat.generate(
        decision=decide(), draft=draft, reask=lambda: None, language="en", nickname=None,
        deadline=time.monotonic() + 1,
    ))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(cancelled) == ["decision", "draft"]


def test_decisions_usage_cache_buckets_are_exclusive():
    assert crisis_chat.ledger_tokens({
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 30},
    }) == {"input_tokens": 30, "cached_input_tokens": 40, "cache_write_tokens": 30}
    assert crisis_chat.ledger_tokens({"input_tokens": 5, "input_tokens_details": {"cached_tokens": 6}}) is None


async def test_slow_ledger_does_not_delay_verdict_or_lose_known_usage(monkeypatch):
    allow_open = asyncio.Event()
    call_id = uuid.uuid4()
    closed = []

    async def open_cost(*a, **kw):
        await allow_open.wait()
        return call_id

    async def close_cost(identity, **kw):
        closed.append((identity, kw))

    async def classify(*a, **kw):
        return crisis_guard.DecisionResult("ok", raw_usage={"input_tokens": 10})

    monkeypatch.setattr(usage_ledger, "open_call", open_cost)
    monkeypatch.setattr(usage_ledger, "close_call", close_cost)
    monkeypatch.setattr(crisis_guard, "decide", classify)
    result = await asyncio.wait_for(crisis_chat.recorded_decision(
        "Synthetic input", [], config=crisis_chat.GuardConfig(),
        deadline=time.monotonic() + 1,
        ledger=usage_ledger.LedgerContext(lane="foreground", purpose="chat"),
    ), timeout=0.2)
    assert result.status == "ok" and closed == []
    allow_open.set()
    await crisis_chat.drain_cost_tasks()
    assert closed[0][0] == call_id
    assert closed[0][1]["input_tokens"] == 10


@pytest.mark.parametrize("language", ["ko", "en", "ja"])
def test_new_role_rule_replaces_legacy_crisis_rule_only_when_enabled(language):
    legacy = prompts.system_prompt(language)
    guarded = prompts.system_prompt(language, crisis_guard_enabled=True)
    assert crisis_guard.base_role_rule(language) in guarded
    assert crisis_guard.base_role_rule(language) not in legacy


@pytest.mark.parametrize(("route", "status"), [("safety", "ok"), ("ask", "ok"), ("normal", "timeout")])
async def test_chat_stores_selected_kind_on_both_messages(monkeypatch, route, status):
    monkeypatch.setattr(chat.settings, "crisis_guard_enabled", True)
    original_config = chat.config_store.get_config_values

    async def config(*args, **kwargs):
        result = await original_config(*args, **kwargs)
        return {**result, "crisis_guard_ready_users": ["11111111-1111-1111-1111-111111111111"]}
    monkeypatch.setattr(chat.config_store, "get_config_values", config)

    async def gating(*a, **kw):
        return _gating()

    async def decide(*a, **kw):
        return crisis_guard.DecisionResult(status, route=route, method_request=False)

    async def model(system, convo, **kw):
        return response("무슨 일 있었어?" if kw.get("ledger").purpose == "crisis_reask" else "일반 답변")

    monkeypatch.setattr(chat.gating, "resolve", gating)
    monkeypatch.setattr(crisis_chat, "recorded_decision", decide)
    monkeypatch.setattr(llm, "generate", model)
    session = FakeSession()
    result = await chat.post_message(
        session, "11111111-1111-1111-1111-111111111111",
        SimpleNamespace(text="Synthetic input", greeting_id=None), "guard-kind",
    )
    messages = [m for m in session.added if isinstance(m, Message)]
    assert [m.kind for m in messages] == [crisis_guard.message_kind(route)] * 2
    assert result.greeting is None
    if route == "safety":
        assert result.reply.content == crisis_guard.fixed_reply("ko", "지훈")
        assert result.reply.references == []
