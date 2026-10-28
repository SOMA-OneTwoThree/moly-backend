"""Installed SDKs, fake HTTP transport: foreground sends once and honors inference budget."""
from __future__ import annotations

import asyncio
import json
from time import monotonic
from types import SimpleNamespace
import uuid

from anthropic import AsyncAnthropic
import httpx
from openai import APITimeoutError, AsyncOpenAI
import pytest

from app.services import llm, usage_ledger
from app.services.agent import runtime
from app.services.agent.config import build_snapshot
from datetime import date

LEDGER = usage_ledger.LedgerContext(lane="foreground", purpose="chat")


@pytest.mark.parametrize("first_status", [200, 500, "timeout", "cancel"])
async def test_physical_attempt_ids_survive_timeout_without_logging_content(
    monkeypatch, ledger, caplog, first_status,
):
    """Every physical send can be traced, including a bounded retry or no headers."""
    sent = []

    async def handler(request):
        sent.append(request)
        if first_status == "cancel":
            raise asyncio.CancelledError()
        if len(sent) == 1 and first_status == "timeout":
            raise httpx.ReadTimeout("private-provider-error", request=request)
        if len(sent) == 1 and first_status == 500:
            return httpx.Response(500, json={"error": {"message": "private-provider-error"}},
                                  headers={"x-request-id": "req-failed"})
        return httpx.Response(200, json=response("openai"), headers={"x-request-id": "req-ok"})

    sdk, _ = install(monkeypatch, "openai", handler)
    caplog.set_level("INFO", logger="app.services.llm")
    try:
        if first_status in {"timeout", "cancel"}:
            error = asyncio.CancelledError if first_status == "cancel" else APITimeoutError
            with pytest.raises(error):
                await invoke("step", deadline=monotonic() + 3)
        else:
            await invoke("step", deadline=monotonic() + 3)
        records = [json.loads(r.getMessage().removeprefix("llm_attempt "))
                   for r in caplog.records if r.getMessage().startswith("llm_attempt ")]
        assert len(records) == len(sent) == (2 if first_status == 500 else 1)
        starts = [json.loads(r.getMessage().removeprefix("llm_attempt_started "))
                  for r in caplog.records if r.getMessage().startswith("llm_attempt_started ")]
        ids = [r.headers["X-Client-Request-Id"] for r in sent]
        assert [r["client_request_id"] for r in starts] == ids
        if first_status == 500:
            assert records[0]["request_id"] == "req-failed"
            assert records[0]["status"] == 500
        assert len(set(ids)) == len(ids)
        assert [r["client_request_id"] for r in records] == ids
        assert [r["attempt"] for r in records] == list(range(1, len(sent) + 1))
        assert all(r["model"] == "gpt-6-luna" and r["max_tokens"] == 64 for r in records)
        assert all(r["latency_ms"] >= 0 and r["timeout_s"] <= 3 for r in records)
        if first_status in {"timeout", "cancel"}:
            assert records[0]["outcome"] == "failed" and records[0]["request_id"] is None
            assert len(ledger["unknown"]) == 1
        else:
            assert records[-1]["outcome"] == "received"
            assert records[-1]["request_id"] == "req-ok"
        assert "private-provider-error" not in caplog.text
        assert not any(k in r for r in records for k in ("messages", "text", "user_id", "api_key"))
    finally:
        await sdk.close()


@pytest.fixture
def ledger(monkeypatch):
    state = {"opened": [], "completed": [], "unknown": [], "failed": []}

    async def open_call(*args, **kwargs):
        key = uuid.uuid4()
        state["opened"].append(key)
        return key

    async def completed(call_id, **kwargs):
        state["completed"].append((call_id, kwargs))

    async def unknown(call_id, **kwargs):
        state["unknown"].append((call_id, kwargs))

    async def failed(call_id, **kwargs):
        state["failed"].append((call_id, kwargs))

    monkeypatch.setattr(usage_ledger, "open_call", open_call)
    monkeypatch.setattr(usage_ledger, "close_call", completed)
    monkeypatch.setattr(usage_ledger, "close_unknown", unknown)
    monkeypatch.setattr(usage_ledger, "close_failed", failed)
    return state


def response(provider):
    if provider == "anthropic":
        return {
            "id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude-sonnet-5", "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }
    return {
        "id": "chatcmpl_test", "object": "chat.completion", "created": 1,
        "model": "gpt-6-luna",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


def install(monkeypatch, provider, handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if provider == "anthropic":
        sdk = AsyncAnthropic(api_key="test-only", base_url="https://provider.invalid", http_client=http)
        monkeypatch.setattr(llm, "_get_client", lambda: sdk)
    else:
        sdk = AsyncOpenAI(api_key="test-only", base_url="https://provider.invalid", http_client=http)
        monkeypatch.setattr(llm, "_get_openai_client", lambda: sdk)
    return sdk, http


async def invoke(mode, *, deadline, ledger_context=LEDGER):
    if mode == "step":
        return await llm.generate_step(
            "system", [llm.UserText("hello")], tools=None, tool_choice="none",
            model="gpt-6-luna", max_tokens=64, timeout=10,
            ledger=ledger_context, foreground_deadline=deadline,
        )
    return await llm.generate(
        "system", [{"role": "user", "content": "hello"}], timeout=10,
        model="claude-sonnet-5" if mode == "anthropic" else "gpt-6-luna",
        ledger=ledger_context, foreground_deadline=deadline,
    )


@pytest.mark.parametrize("mode", ["openai", "step", "anthropic"])
@pytest.mark.parametrize("failure", [429, 500, "read_timeout"])
async def test_foreground_sdk_fault_makes_one_physical_send(monkeypatch, ledger, mode, failure):
    sends = []

    async def handler(request):
        sends.append(request)
        if failure == "read_timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(failure, json={"error": {"message": "synthetic", "type": "api_error"}})

    sdk, http = install(monkeypatch, "anthropic" if mode == "anthropic" else "openai", handler)
    try:
        with pytest.raises(Exception):
            await invoke(mode, deadline=monotonic() + 1)
        assert len(sends) == 1
        assert sdk.max_retries == 2  # shared client defaults were not mutated
        assert not http.is_closed
        assert len(ledger["unknown"]) == 1
        assert ledger["failed"] == ledger["completed"] == []
    finally:
        await sdk.close()


@pytest.mark.parametrize("mode", ["openai", "step", "anthropic"])
async def test_outer_deadline_cancels_provider_wait_once(monkeypatch, ledger, mode):
    sends = []
    cancelled = []

    async def handler(request):
        sends.append(request)
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    sdk, _ = install(monkeypatch, "anthropic" if mode == "anthropic" else "openai", handler)
    try:
        with pytest.raises(TimeoutError):
            await invoke(mode, deadline=monotonic() + 0.03)
        assert len(sends) == len(cancelled) == 1
        assert ledger["unknown"][0][1]["error_code"] == "foreground_timeout"
        assert ledger["completed"] == ledger["failed"] == []
    finally:
        await sdk.close()


@pytest.mark.parametrize("mode", ["openai", "step", "anthropic"])
async def test_external_cancel_propagates_and_preserves_unknown_usage(monkeypatch, ledger, mode):
    entered = asyncio.Event()
    sends = []

    async def handler(request):
        sends.append(request)
        entered.set()
        await asyncio.sleep(10)

    sdk, _ = install(monkeypatch, "anthropic" if mode == "anthropic" else "openai", handler)
    try:
        task = asyncio.create_task(invoke(mode, deadline=monotonic() + 10))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(sends) == 1
        assert ledger["unknown"][0][1]["error_code"] == "foreground_cancelled"
        assert ledger["completed"] == ledger["failed"] == []
    finally:
        await sdk.close()


@pytest.mark.parametrize("mode", ["openai", "step", "anthropic"])
async def test_ledger_start_delay_rechecks_deadline_before_send(monkeypatch, ledger, mode):
    sends = []

    async def handler(request):
        sends.append(request)
        return httpx.Response(200, json=response("anthropic" if mode == "anthropic" else "openai"))

    async def delayed_open(*args, **kwargs):
        await asyncio.sleep(0.03)
        return uuid.uuid4()

    monkeypatch.setattr(usage_ledger, "open_call", delayed_open)
    sdk, _ = install(monkeypatch, "anthropic" if mode == "anthropic" else "openai", handler)
    try:
        with pytest.raises(TimeoutError):
            await invoke(mode, deadline=monotonic() + 0.01)
        assert sends == []
        assert ledger["failed"][0][1]["error_code"] == "deadline_before_send"
        assert ledger["unknown"] == ledger["completed"] == []
    finally:
        await sdk.close()


@pytest.mark.parametrize("mode", ["openai", "step", "anthropic"])
async def test_received_usage_finishes_even_when_ledger_close_crosses_deadline(monkeypatch, ledger, mode):
    sends = []

    async def handler(request):
        sends.append(request)
        return httpx.Response(200, json=response("anthropic" if mode == "anthropic" else "openai"))

    async def delayed_close(call_id, **kwargs):
        await asyncio.sleep(0.25)
        ledger["completed"].append((call_id, kwargs))

    monkeypatch.setattr(usage_ledger, "close_call", delayed_close)
    sdk, _ = install(monkeypatch, "anthropic" if mode == "anthropic" else "openai", handler)
    try:
        result = await invoke(mode, deadline=monotonic() + 0.2)
        assert result.text == "ok" and len(sends) == 1
        assert ledger["completed"][0][1]["input_tokens"] == 12
        assert ledger["completed"][0][1]["output_tokens"] == 3
        assert ledger["completed"][0][1]["preserve_on_cancel"] is True
        assert ledger["unknown"] == ledger["failed"] == []
    finally:
        await sdk.close()


async def test_chat_cancel_records_unknown_releases_lease_and_does_not_charge_or_store(monkeypatch, ledger):
    from app.services import chat, gating
    from app.services.agent import config
    from tests.test_chat import FakeSession, _gating

    entered = asyncio.Event()
    released = []
    charged = []

    class Session(FakeSession):
        rollbacks = 0

        async def rollback(self):
            self.rollbacks += 1

    async def handler(request):
        entered.set()
        await asyncio.sleep(10)

    async def resolve(*args, **kwargs):
        return _gating()

    async def snapshot(*args, **kwargs):
        return build_snapshot({"agent_enabled": False})

    async def release(session, **kwargs):
        released.append(kwargs)

    async def accumulate(*args, **kwargs):
        charged.append(kwargs)
        pytest.fail("cancelled inference must not charge success quota")

    monkeypatch.setattr(gating, "resolve", resolve)
    monkeypatch.setattr(config, "effective_agent_config", snapshot)
    monkeypatch.setattr(chat.chat_turns, "release", release)
    monkeypatch.setattr(chat, "_accumulate_tokens", accumulate)
    monkeypatch.setattr(llm.settings, "model_chat", "gpt-6-luna")
    sdk, _ = install(monkeypatch, "openai", handler)
    session = Session()
    try:
        task = asyncio.create_task(chat.post_message(
            session, str(_gating().profile.id), SimpleNamespace(text="hello", greeting_id=None),
            "synthetic-cancel-test",
        ))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.rollbacks == len(released) == 1
        assert session.added == charged == []
        assert len(ledger["unknown"]) == 1
        assert ledger["completed"] == ledger["failed"] == []
    finally:
        await sdk.close()


async def test_chat_cancel_during_timeout_ledger_cleanup_never_starts_fallback(monkeypatch):
    """A timeout is not permission to retry after cancellation during its cleanup."""
    from app.core import db as database
    from app.services import chat, gating
    from app.services.agent import config
    from tests.test_chat import FakeSession, _gating

    cleanup_entered = asyncio.Event()
    sends, released, charged, completed = [], [], [], []
    rows = {}

    class Session(FakeSession):
        rollbacks = 0

        async def rollback(self):
            self.rollbacks += 1

    class LedgerSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def commit(self):
            return None

    class Registry:
        def wire_schemas(self):
            return [{"type": "function", "function": {
                "name": "read_only", "parameters": {"type": "object", "properties": {}},
            }}]

        def input_models(self):
            return {}

        def get(self, name):
            pytest.fail("cancelled decide cleanup must not execute tools")

    async def handler(request):
        sends.append(request)
        if len(sends) == 1:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(200, json=response("openai"))

    async def opened(*args, **kwargs):
        call_id = uuid.uuid4()
        rows[call_id] = "started"
        return call_id

    async def mark_unknown(session, call_id, **kwargs):
        cleanup_entered.set()
        await asyncio.sleep(10)
        rows[call_id] = "unknown_usage"

    async def close_call(*args, **kwargs):
        completed.append(kwargs)

    async def resolve(*args, **kwargs):
        return _gating()

    async def snapshot(*args, **kwargs):
        return build_snapshot({"agent_enabled": True, "agent_canary_pct": 100})

    async def recall(*args, **kwargs):
        return ""

    async def release(session, **kwargs):
        released.append(kwargs)

    async def accumulate(*args, **kwargs):
        charged.append(kwargs)
        return None

    monkeypatch.setattr(usage_ledger, "open_call", opened)
    monkeypatch.setattr(usage_ledger, "mark_unknown", mark_unknown)
    monkeypatch.setattr(usage_ledger, "close_call", close_call)
    monkeypatch.setattr(database, "get_sessionmaker", lambda: LedgerSession)
    monkeypatch.setattr(gating, "resolve", resolve)
    monkeypatch.setattr(config, "effective_agent_config", snapshot)
    monkeypatch.setattr(runtime, "_resolve_registry", Registry)
    monkeypatch.setattr(chat, "_recall_memory_v2", recall)
    monkeypatch.setattr(chat.chat_turns, "release", release)
    monkeypatch.setattr(chat, "_accumulate_tokens", accumulate)
    monkeypatch.setattr(llm.settings, "model_chat", "gpt-6-luna")
    sdk, _ = install(monkeypatch, "openai", handler)
    session = Session()
    try:
        task = asyncio.create_task(chat.post_message(
            session, str(_gating().profile.id), SimpleNamespace(text="hello", greeting_id=None),
            "synthetic-cleanup-cancel-test",
        ))
        await asyncio.wait_for(cleanup_entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(sends) == 1
        assert list(rows.values()) == ["started"]  # Existing reconciler retains ownership.
        assert session.rollbacks == len(released) == 1
        assert session.added == charged == completed == []
    finally:
        await sdk.close()


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
async def test_recall_boundary_propagates_parent_cancel_but_keeps_timeout_fallback(monkeypatch, ledger, mode):
    from app.services import chat, gating
    from app.services.agent import config
    from tests.test_chat import FakeSession, _gating

    entered = asyncio.Event()
    recall_cancelled = []
    sends = []
    released = []
    charged = []

    class Session(FakeSession):
        rollbacks = 0

        async def rollback(self):
            self.rollbacks += 1

    async def recall(*args, **kwargs):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            recall_cancelled.append(True)

    async def handler(request):
        sends.append(request)
        return httpx.Response(200, json=response("openai"))

    async def resolve(*args, **kwargs):
        return _gating()

    async def snapshot(*args, **kwargs):
        return build_snapshot({"agent_enabled": False})

    async def release(session, **kwargs):
        released.append(kwargs)

    async def accumulate(*args, **kwargs):
        charged.append(kwargs)
        return None

    monkeypatch.setattr(chat, "_recall_memory_v2", recall)
    monkeypatch.setattr(gating, "resolve", resolve)
    monkeypatch.setattr(config, "effective_agent_config", snapshot)
    monkeypatch.setattr(chat.chat_turns, "release", release)
    monkeypatch.setattr(chat, "_accumulate_tokens", accumulate)
    monkeypatch.setattr(llm.settings, "model_chat", "gpt-6-luna")
    if mode == "timeout":
        monkeypatch.setattr(chat, "_MEM0_RECALL_TIMEOUT_S", 0.03)
    sdk, _ = install(monkeypatch, "openai", handler)
    session = Session()
    try:
        task = asyncio.create_task(chat.post_message(
            session, str(_gating().profile.id),
            SimpleNamespace(text="내가 말한 이름 기억나?", greeting_id=None),
            "synthetic-recall-boundary",
        ))
        await asyncio.wait_for(entered.wait(), 1)
        if mode == "cancel":
            assert session.committed  # cancellation happens while collecting after Phase 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert session.rollbacks == len(released) == 1
            assert session.added == charged == sends == ledger["opened"] == []
        else:
            assert (await task).reply.content == "ok"
            assert len(sends) == len(charged) == len(ledger["completed"]) == 1
        assert recall_cancelled == [True]
    finally:
        await sdk.close()


@pytest.mark.parametrize("mode", ["openai", "anthropic"])
async def test_background_keeps_original_sdk_retry_behavior(monkeypatch, ledger, mode):
    sends = []

    async def handler(request):
        sends.append(request)
        if len(sends) == 1:
            return httpx.Response(500, json={"error": {"message": "synthetic", "type": "api_error"}})
        return httpx.Response(200, json=response(mode))

    sdk, _ = install(monkeypatch, mode, handler)
    monkeypatch.setattr(sdk, "_calculate_retry_timeout", lambda *args: 0)
    try:
        result = await invoke(mode, deadline=None)
        assert result.text == "ok" and len(sends) == 2
        assert sdk.max_retries == 2
        assert len(ledger["completed"]) == 1 and ledger["unknown"] == []
        assert "preserve_on_cancel" not in ledger["completed"][0][1]
    finally:
        await sdk.close()


async def test_timeout_fallback_has_two_sdk_sends_total_and_does_not_execute_tools(monkeypatch, ledger):
    sends = []

    async def handler(request):
        sends.append(request)
        if len(sends) == 1:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(200, json=response("openai"))

    class Registry:
        def wire_schemas(self):
            return [{"type": "function", "function": {
                "name": "read_only", "parameters": {"type": "object", "properties": {}},
            }}]

        def input_models(self):
            return {}

        def get(self, name):
            pytest.fail("a timed-out decide step must not execute tools")

    sdk, _ = install(monkeypatch, "openai", handler)
    try:
        result = await runtime.run_turn(
            "system", [{"role": "user", "content": "hello"}],
            config=build_snapshot({"agent_enabled": True, "agent_canary_pct": 100}),
            user_id=uuid.uuid4(), language="en", activity_date=date(2026, 10, 14),
            user_text="hello", model="gpt-6-luna", registry=Registry(),
        )
        assert result.text == "ok" and result.skipped == "deadline"
        assert len(sends) == 2
        assert len(ledger["unknown"]) == len(ledger["completed"]) == 1
        assert ledger["failed"] == []
    finally:
        await sdk.close()


@pytest.mark.parametrize("final_status", [200, 500])
async def test_read_tool_runs_once_and_final_keeps_main_bounded_retry(
    monkeypatch, ledger, final_status,
):
    from tests.test_agent_runtime import _FakeRegistry, _FakeTool, _factory

    sends = []
    tool = _FakeTool("read_only")

    async def handler(request):
        sends.append(request)
        if len(sends) == 1:
            first = response("openai")
            first["choices"][0]["message"].update({
                "content": None,
                "tool_calls": [{"id": "read_call", "type": "function", "function": {
                    "name": "read_only", "arguments": "{}",
                }}],
            })
            first["choices"][0]["finish_reason"] = "tool_calls"
            return httpx.Response(200, json=first)
        if final_status == 500:
            return httpx.Response(500, json={"error": {"message": "synthetic", "type": "api_error"}})
        return httpx.Response(200, json=response("openai"))

    sdk, _ = install(monkeypatch, "openai", handler)
    try:
        turn = runtime.run_turn(
            "system", [{"role": "user", "content": "hello"}],
            config=build_snapshot({"agent_enabled": True, "agent_canary_pct": 100}),
            user_id=uuid.uuid4(), language="en", activity_date=date(2026, 10, 14),
            user_text="hello", model="gpt-6-luna", registry=_FakeRegistry(tool),
            session_factory=_factory,
        )
        if final_status == 500:
            with pytest.raises(Exception):
                await turn
            assert len(ledger["unknown"]) == len(ledger["completed"]) == 1
        else:
            assert (await turn).text == "ok"
            assert len(ledger["completed"]) == 2 and ledger["unknown"] == []
        assert len(sends) == (3 if final_status == 500 else 2) and tool.ran == 1
        assert ledger["failed"] == []
    finally:
        await sdk.close()


@pytest.mark.parametrize("initial_failure", ["length", "timeout"])
@pytest.mark.parametrize("recovery_finishes", [True, False])
@pytest.mark.parametrize("usage_v2", [True, False])
async def test_recovery_preserves_company_cost_and_user_quota(
    monkeypatch, ledger, recovery_finishes, usage_v2, initial_failure,
):
    """Real runtime + SDK + chat publication; fake HTTP/DB, no paid provider calls."""
    from app.core.errors import AppError
    from app.models.idempotency_key import IdempotencyKey
    from app.models.message import Message
    from app.services import chat
    from tests.test_agent_runtime import _FakeRegistry, _FakeTool, _post
    from tests.test_chat import FakeSession

    sends, charges = [], []
    tool = _FakeTool()
    monkeypatch.setattr(runtime, "_registry_override", _FakeRegistry(tool))
    monkeypatch.setattr(runtime, "should_run", lambda *args: True)
    monkeypatch.setattr(llm.settings, "llm_max_tokens", 1024)
    monkeypatch.setattr(llm.settings, "model_chat", "gpt-6-luna")
    monkeypatch.setattr(chat.settings, "turn_usage_v2_enabled", usage_v2)

    async def accumulate(session, user_id, activity_date, consumed):
        charges.append(consumed)

    monkeypatch.setattr(chat, "_accumulate_tokens", accumulate)

    async def handler(request):
        sends.append(json.loads(request.content))
        if len(sends) == 1 and initial_failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        payload = response("openai")
        truncated = len(sends) == 1 or not recovery_finishes
        payload["choices"][0]["finish_reason"] = "length" if truncated else "stop"
        payload["choices"][0]["message"]["content"] = "잘린 답변" if truncated else "완성된 답변이야."
        payload["usage"]["completion_tokens"] = 192 if truncated else 3
        return httpx.Response(200, json=payload)

    sdk, _ = install(monkeypatch, "openai", handler)
    session = FakeSession()
    try:
        operation = _post(session, monkeypatch)
        if recovery_finishes:
            out = await operation
            expected = llm.billable_tokens(llm.LLMResult(
                text="완성된 답변이야.", input_tokens=12, output_tokens=3, model="gpt-6-luna",
            ))
            assert out.tokens_used == 1000 + expected
            assert charges == [expected]
            messages = [m for m in session.added if isinstance(m, Message)]
            assert len(messages) == 2  # One user/assistant pair, no partial reply.
            reply = next(m for m in messages if m.sender == "moly")
            assert reply.content == "완성된 답변이야."
            assert reply.billable_tokens == expected
            assert reply.output_tokens == 3
            cached = next(m for m in session.added if isinstance(m, IdempotencyKey))
            replay_session = FakeSession(get_map={"IdempotencyKey": cached})
            replay = await _post(replay_session, monkeypatch)
            assert replay.model_dump(mode="json") == out.model_dump(mode="json")
            assert replay_session.added == []
            assert charges == [expected]  # Retrying the HTTP request cannot charge twice.
        else:
            with pytest.raises(AppError) as caught:
                await operation
            assert caught.value.code == "AI_UNAVAILABLE"
            assert charges == session.added == []
        assert tool.ran == 0
        assert len(sends) == 2
        assert [s["max_completion_tokens"] for s in sends] == [192, 1024]
        if initial_failure == "length":
            assert sends[0]["messages"] == sends[1]["messages"][:-1]
            assert sends[1]["messages"][-1]["role"] == "system"
            assert "[Length recovery]" in sends[1]["messages"][-1]["content"]
        else:
            assert sends[0]["messages"] == sends[1]["messages"]
        assert "tools" not in sends[1]
        assert len(ledger["opened"]) == 2
        assert len(ledger["unknown"]) == (1 if initial_failure == "timeout" else 0)
        expected_outputs = ([] if initial_failure == "timeout" else [192])
        expected_outputs.append(3 if recovery_finishes else 192)
        assert [row[1]["output_tokens"] for row in ledger["completed"]] == expected_outputs
    finally:
        await sdk.close()
