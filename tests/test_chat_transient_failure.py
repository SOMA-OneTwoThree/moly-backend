"""Inference failure cleanup precedes retryable errors; never publish a canned reply."""
import asyncio
import logging
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.services import chat, gating, llm
from app.services.agent import runtime as agent_runtime
from tests.test_chat import UID, FakeSession, _gating


def _provider_error(status):
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://provider.invalid"),
        headers={"x-request-id": "private-request-identifier"},
    )
    return openai.APIStatusError("private-provider-body", response=response, body=None)


class _TrackedSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.events = []

    async def rollback(self):
        self.events.append("rollback")

    async def commit(self):
        self.events.append("commit")
        await super().commit()


def _failure_setup(monkeypatch, session, failure, *, agent=False):
    calls = []

    async def resolve(*args, **kwargs):
        return _gating()

    async def fail(*args, **kwargs):
        calls.append("inference")
        raise failure

    async def release(*args, **kwargs):
        session.events.append("release")

    async def no_charge(*args, **kwargs):
        pytest.fail("failed inference must not consume user quota")

    monkeypatch.setattr(gating, "resolve", resolve)
    monkeypatch.setattr(chat.chat_turns, "release", release)
    monkeypatch.setattr(chat, "_accumulate_tokens", no_charge)
    monkeypatch.setattr(agent_runtime, "should_run", lambda *args: agent)
    if agent:
        monkeypatch.setattr(agent_runtime, "run_turn", fail)
    monkeypatch.setattr(llm, "generate", fail)
    return calls


@pytest.mark.parametrize("agent", (False, True))
@pytest.mark.parametrize("failure", (
    TimeoutError("private-provider-body"),
    openai.APIConnectionError(request=httpx.Request("POST", "https://provider.invalid")),
    _provider_error(429), _provider_error(503),
))
async def test_transient_failure_cleans_up_before_503_without_retry_or_publication(
    monkeypatch, caplog, failure, agent,
):
    session = _TrackedSession()
    calls = _failure_setup(monkeypatch, session, failure, agent=agent)
    with caplog.at_level(logging.WARNING, logger="moly-backend"):
        with pytest.raises(AppError) as caught:
            await chat.post_message(
                session, UID, SimpleNamespace(text="안녕", greeting_id=None), "transient-failure",
            )
    assert caught.value.code == "AI_UNAVAILABLE"
    assert caught.value.http_status == 503
    assert caught.value.details == {"retryable": True}
    assert caught.value.__cause__ is failure
    assert session.events[-3:] == ["rollback", "release", "commit"]
    assert calls == ["inference"]
    assert session.added == []  # No message, idempotent success response or quota record.
    assert "private-provider-body" not in caplog.text
    assert "private-request-identifier" not in caplog.text


@pytest.mark.parametrize("failure", (
    _provider_error(400), _provider_error(401), _provider_error(404),
    ValueError("application bug"), asyncio.CancelledError(),
    AppError("LIMIT", 429, "application limit"),
))
async def test_non_transient_failure_keeps_original_exception_and_cleanup(monkeypatch, failure):
    session = _TrackedSession()
    calls = _failure_setup(monkeypatch, session, failure)
    with pytest.raises(type(failure)) as caught:
        await chat.post_message(
            session, UID, SimpleNamespace(text="안녕", greeting_id=None), "non-transient-failure",
        )
    assert caught.value is failure
    assert session.events[-3:] == ["rollback", "release", "commit"]
    assert calls == ["inference"]
    assert session.added == []


def test_chat_api_returns_retryable_envelope_without_assistant_response(monkeypatch):
    session = _TrackedSession()
    calls = _failure_setup(monkeypatch, session, TimeoutError("private-provider-body"))

    async def dependency():
        yield session

    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: UID)
    monkeypatch.setitem(app.dependency_overrides, get_session, dependency)
    response = TestClient(app).post(
        "/chat/messages", json={"text": "안녕"}, headers={"Idempotency-Key": "api-transient"},
    )
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "AI_UNAVAILABLE",
            "message": "캐피가 잠깐 대답하기 어려워요. 조금 뒤에 다시 보내 주세요.",
            "details": {"retryable": True},
        },
    }
    assert session.events[-3:] == ["rollback", "release", "commit"]
    assert calls == ["inference"]
    assert session.added == []


@pytest.mark.parametrize("text,finish_reason", [("partial", "length"), ("", "stop"), ("……", "stop")])
def test_chat_api_rejects_incomplete_or_cleaned_empty_reply(monkeypatch, text, finish_reason):
    session = _TrackedSession()
    calls = _failure_setup(monkeypatch, session, TimeoutError())

    async def completed(*args, **kwargs):
        calls.append("inference")
        return llm.LLMResult(text=text, finish_reason=finish_reason, input_tokens=5, output_tokens=2)

    async def dependency():
        yield session

    monkeypatch.setattr(llm, "generate", completed)
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: UID)
    monkeypatch.setitem(app.dependency_overrides, get_session, dependency)
    result = TestClient(app).post("/chat/messages", json={"text": "응"}, headers={"Idempotency-Key": "incomplete"})
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "AI_UNAVAILABLE"
    assert result.json()["error"]["details"] == {"retryable": True}
    assert calls == ["inference"]
    assert session.events[-3:] == ["rollback", "release", "commit"]
    assert session.added == []
