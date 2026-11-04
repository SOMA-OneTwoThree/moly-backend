"""Body-free recall outcome observations; fake DB/model only."""
import hashlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import db as app_db
from app.services import chat, crisis_chat, crisis_guard, provided_memory
from tests.test_guard_context_v2_integration_review import Session, request, setup as setup
from tests.test_chat import UID

ACTUAL_RECALL = chat._recall_memory_v2
SUBJECT = hashlib.sha256(str(UID).encode()).hexdigest()
PRIVATE_QUERY = "synthetic-private-query-never-in-observation"
PRIVATE_FACT = "synthetic-private-memory-never-in-observation"


@pytest.mark.parametrize("outcome", ["provided", "empty", "unavailable", "not_enabled", "timeout"])
async def test_normal_outcome_is_body_free_and_stage_values_remain_floats(
    setup, monkeypatch, caplog, outcome,
):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="normal", case="not_applicable")))
    monkeypatch.setattr(chat.memory_pipeline, "load", AsyncMock(return_value=SimpleNamespace(
        serves_v2=outcome != "not_enabled")))
    monkeypatch.setattr(provided_memory, "validate_for_publish", AsyncMock(return_value=True))
    trace_seen = []

    async def recall(*args, **kwargs):
        trace = kwargs["trace"]
        trace.update(embed_s=0.1, search_s=0.2, registry_s=0.3)
        trace_seen.append(trace)
        if outcome in {"provided", "timeout", "empty"}:
            kwargs["registry_snapshot"].append(SimpleNamespace(canonical_text=PRIVATE_FACT))
        if outcome == "timeout":
            raise TimeoutError
        if outcome == "unavailable":
            return chat._memory_unavailable_notice(kwargs["language"])
        return PRIVATE_FACT if outcome == "provided" else ""

    monkeypatch.setattr(chat, "_recall_memory_v2", recall)
    with caplog.at_level(logging.INFO, logger=chat._log.name):
        await chat.post_message(Session(), UID, request(PRIVATE_QUERY), "body-free-recall-test")
    entries = [r.getMessage() for r in caplog.records if r.getMessage().startswith("context_recall_outcome ")]
    assert len(entries) == 1
    expected = "unavailable" if outcome == "timeout" else outcome
    assert f"subject={SUBJECT} state={expected} provided={int(outcome == 'provided')} " in entries[0]
    assert " elapsed=" in entries[0] and " stages=" in entries[0]
    assert str(UID) not in entries[0]
    assert PRIVATE_QUERY not in entries[0] and PRIVATE_FACT not in entries[0]
    assert trace_seen and all(type(value) is float for value in trace_seen[0].values())
    assert set(trace_seen[0]) == {"embed_s", "search_s", "registry_s"}


async def test_block_failure_warning_has_owner_and_type_but_no_exception_or_query(monkeypatch, caplog):
    secret = "synthetic-provider-credential-and-body"

    class FailedSession:
        async def __aenter__(self):
            raise RuntimeError(secret)

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(app_db, "get_sessionmaker", lambda: FailedSession)
    proofs = [object()]
    with caplog.at_level(logging.WARNING, logger=chat._log.name):
        result = await ACTUAL_RECALL(chat._uid(UID), query=PRIVATE_QUERY,
            state=SimpleNamespace(serves_v2=True), language="ko", retention_enabled=True,
            registry_snapshot=proofs)
    entries = [r.getMessage() for r in caplog.records if r.getMessage().startswith("v2 회상 블록 생성 실패 ")]
    assert entries == [f"v2 회상 블록 생성 실패 subject={SUBJECT} error_type=RuntimeError"]
    assert str(UID) not in entries[0]
    assert secret not in entries[0] and PRIVATE_QUERY not in entries[0]
    assert not proofs and result == chat._memory_unavailable_notice("ko")


@pytest.mark.parametrize("route", ["ask", "safety"])
async def test_fixed_early_return_emits_no_normal_recall_outcome(setup, monkeypatch, caplog, route):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route=route, case="self_thought")))
    with caplog.at_level(logging.INFO, logger=chat._log.name):
        await chat.post_message(Session(), UID, request(), "fixed-no-recall-outcome")
    assert not any(r.getMessage().startswith("context_recall_outcome ") for r in caplog.records)
    chat._recall_memory_v2.assert_not_awaited()


async def test_unadopted_retention_off_emits_no_outcome(setup, monkeypatch, caplog):
    _, configuration = setup
    configuration["context_retention_v2_enabled"] = False
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="normal", case="not_applicable")))
    with caplog.at_level(logging.INFO, logger=chat._log.name):
        await chat.post_message(Session(), UID, request(), "off-no-recall-outcome")
    assert not any(r.getMessage().startswith("context_recall_outcome ") for r in caplog.records)


async def test_outcome_handler_failure_cannot_fail_normal_reply(setup, monkeypatch):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="normal", case="not_applicable")))
    observed = []

    class FailedHandler(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith("context_recall_outcome "):
                observed.append(record.getMessage())
                raise RuntimeError("synthetic serialization failure")

    handler = FailedHandler()
    previous = chat._log.level
    chat._log.setLevel(logging.INFO)
    chat._log.addHandler(handler)
    try:
        reply = await chat.post_message(Session(), UID, request(), "recall-log-failure")
    finally:
        chat._log.removeHandler(handler)
        chat._log.setLevel(previous)
    assert len(observed) == 1
    assert reply.reply.content == "합성 답변이야."
    chat.llm.generate.assert_awaited_once()
