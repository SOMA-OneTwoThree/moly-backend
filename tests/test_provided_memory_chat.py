"""Publication proof failures cannot write or charge; only the owned lease is released."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import errors
from app.models.message import Message
from app.services import chat, crisis_chat, crisis_guard, provided_memory
from tests.test_guard_context_v2_integration_review import setup as setup, Session, request, UID


@pytest.fixture
def provided_turn(setup, monkeypatch):
    marker = SimpleNamespace(name="server_owned_proof")

    async def recall(*args, **kw):
        kw["registry_snapshot"].append(marker)
        return "verified optional memory"

    monkeypatch.setattr(chat, "_recall_memory_v2", AsyncMock(side_effect=recall))
    monkeypatch.setattr(
        crisis_chat,
        "recorded_decision",
        AsyncMock(
            return_value=crisis_guard.DecisionResult("ok", route="normal", case="not_applicable")
        ),
    )
    validate = AsyncMock(return_value=True)
    monkeypatch.setattr(provided_memory, "validate_for_publish", validate)
    return marker, validate


async def test_exact_provided_proof_is_checked_before_one_publication(provided_turn):
    marker, validate = provided_turn
    session = Session()
    await chat.post_message(session, UID, request(), "provided-once")
    validate.assert_awaited_once()
    assert validate.await_args.kwargs["snapshots"] == (marker,)
    assert len([x for x in session.added if isinstance(x, Message)]) == 2
    chat._accumulate_tokens.assert_awaited_once()


@pytest.mark.parametrize("failure", ["stale", "source_invalid", "cancel"])
async def test_proof_rejection_or_cancel_never_writes_or_charges(provided_turn, failure):
    _, validate = provided_turn
    if failure == "stale":
        validate.return_value = False
    elif failure == "source_invalid":
        validate.side_effect = provided_memory.ProvidedMemoryError("synthetic")
    else:
        validate.side_effect = asyncio.CancelledError()
    session = Session()
    with pytest.raises((errors.AppError, asyncio.CancelledError)):
        await chat.post_message(session, UID, request(), "provided-rejected")
    assert session.added == []
    chat._accumulate_tokens.assert_not_awaited()
    chat.chat_turns.release.assert_awaited_once()


@pytest.mark.parametrize("state", ["55P03", "40P01", "57014"])
async def test_late_final_write_lock_conflict_is_503_and_releases_only_own_lease(
    monkeypatch, state
):
    class Failure(RuntimeError):
        sqlstate = state

    release = AsyncMock()
    monkeypatch.setattr(chat.chat_turns, "release", release)

    async def inner(session, user_id, req, key, **kwargs):
        kwargs["_owner"].update(lease="original-owned-token", provided_memory_protected=True)
        raise Failure("synthetic")

    monkeypatch.setattr(chat, "_post_message_impl", inner)
    with pytest.raises(errors.AppError) as error:
        await chat.post_message(Session(), UID, request(), "late-lock-conflict")
    assert error.value.code == "AI_UNAVAILABLE" and error.value.http_status == 503
    assert release.await_args.kwargs["lease"] == "original-owned-token"
    release.assert_awaited_once()


async def test_nonlock_failure_is_not_misreported_as_retryable_provider_failure(monkeypatch):
    async def inner(session, user_id, req, key, **kwargs):
        kwargs["_owner"].update(lease="owned", provided_memory_protected=True)
        raise RuntimeError("synthetic coding error")

    monkeypatch.setattr(chat, "_post_message_impl", inner)
    monkeypatch.setattr(chat.chat_turns, "release", AsyncMock())
    with pytest.raises(RuntimeError):
        await chat.post_message(Session(), UID, request(), "nonlock-error")
