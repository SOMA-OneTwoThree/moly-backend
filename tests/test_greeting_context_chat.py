"""Public chat wiring: displayed opening, guard proof and atomic publication."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.core import errors
from app.core.content_crypto import ContentCryptoError
from app.models.greeting import Greeting
from app.models.message import Message
from app.services import chat, crisis_chat, crisis_guard, greeting_context, llm
from tests.test_chat import UID
from tests.test_guard_context_v2_integration_review import Session, request, setup as _setup


setup = _setup


def row(**overrides):
    return SimpleNamespace(**(dict(id=uuid.uuid4(), user_id=uuid.UUID(UID),
        content="방금 밥 먹고 걷다 왔어.", content_ciphertext=None,
        created_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
        activity_date=date(2026, 11, 5), committed_message_id=None) | overrides))


class OpeningSession(Session):
    def __init__(self, opening):
        super().__init__()
        self.opening = opening
        self.fresh = None

    async def get(self, model, key, **kwargs):
        if model is Greeting:
            return self.fresh if kwargs.get("populate_existing") and self.fresh is not None else self.opening
        return await super().get(model, key, **kwargs)


def normal_context(monkeypatch, current="나도"):
    monkeypatch.setattr(chat, "_context", AsyncMock(return_value=chat.ContextWindow(
        conversation=[{"role": "user", "content": "내일 아침 메뉴 추천해줘"},
                      {"role": "assistant", "content": "에그인헬은 어때?"},
                      {"role": "user", "content": current}],
        new_anchor=None, lead=[], checkpoint_keep_from=None, incomplete=False)))
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="normal", case="not_applicable")))


@pytest.mark.parametrize("language", ["ko", "en", "ja"])
async def test_opening_is_immediately_before_reply_and_commits_once(setup, monkeypatch, language):
    gate, _ = setup
    gate.profile.language = language
    normal_context(monkeypatch)
    chat.llm.generate.return_value = llm.LLMResult(
        {"ko": "산책했구나.", "en": "You went for a walk too.", "ja": "散歩してきたんだね。"}[language],
        10, 20, finish_reason="stop")
    opening = row()
    session = OpeningSession(opening)
    req = request("나도")
    req.greeting_id = str(opening.id)
    result = await chat.post_message(session, UID, req, f"opening-{language}")
    system, messages = chat.llm.generate.await_args.args[:2]
    assert messages[-2] == {"role": "assistant", "content": opening.content}
    assert "나도" in messages[-1]["content"] and messages[-1]["role"] == "user"
    assert sum(m["content"] == opening.content for m in messages) == 1
    assert opening.content not in "\n".join(system)
    assert any("에그인헬" in m["content"] for m in messages)
    assert sum(greeting_context.reference_note(language) in m["content"] for m in messages) == 1
    guard_recent = crisis_chat.recorded_decision.await_args.args[1]
    assert [(m.role, m.content, m.kind, m.message_id) for m in guard_recent] == [
        ("assistant", opening.content, "greeting", None)]
    assert chat._guard_recent.return_value == []  # DB-only publication proof stays unchanged.
    stored = [m for m in session.added if isinstance(m, Message)]
    assert [m.kind for m in stored] == ["greeting", "normal", "normal"]
    assert stored[0].turn_position == 0 and opening.committed_message_id == stored[0].id
    assert result.greeting.content == opening.content
    assert all(greeting_context.reference_note(language) not in m.content for m in stored)


@pytest.mark.parametrize("foreign,committed", [(True, False), (False, True)])
async def test_ineligible_opening_is_not_decoded_or_attached(setup, monkeypatch, foreign, committed):
    normal_context(monkeypatch)
    opening = row(user_id=uuid.uuid4() if foreign else uuid.UUID(UID),
                  committed_message_id=12 if committed else None)
    decode = AsyncMock(side_effect=AssertionError("must not decode foreign or already committed opening"))
    monkeypatch.setattr(chat, "_greeting_text", decode)
    req = request("나도")
    req.greeting_id = str(opening.id)
    result = await chat.post_message(OpeningSession(opening), UID, req, "opening-ineligible")
    assert result.greeting is None and decode.call_count == 0
    assert all(m["content"] != opening.content for m in chat.llm.generate.await_args.args[1])


@pytest.mark.parametrize("mutation", ["text", "commit", "owner", "issued"])
async def test_opening_changed_during_generation_cannot_publish(setup, monkeypatch, mutation):
    normal_context(monkeypatch)
    opening = row()
    session = OpeningSession(opening)
    changed = dict(vars(opening))
    changed.update({"text": {"content": "다른 인사"}, "commit": {"committed_message_id": 99},
                    "owner": {"user_id": uuid.uuid4()},
                    "issued": {"created_at": datetime(2026, 11, 6, tzinfo=timezone.utc)}}[mutation])
    session.fresh = SimpleNamespace(**changed)
    req = request("나도")
    req.greeting_id = str(opening.id)
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(session, UID, req, "opening-stale")
    assert caught.value.code == "CHAT_TURN_STALE"
    assert not any(isinstance(m, Message) for m in session.added)
    assert opening.committed_message_id is None


@pytest.mark.parametrize("route", ["ask", "safety"])
@pytest.mark.parametrize("bad_reference", ["uuid", "cipher"])
async def test_bad_opening_does_not_block_existing_fixed_guard(setup, monkeypatch, route, bad_reference):
    opening = row()
    req = request("합성 안전 입력")
    req.greeting_id = "bad-id" if bad_reference == "uuid" else str(opening.id)
    if bad_reference == "cipher":
        monkeypatch.setattr(chat, "_greeting_text", lambda _: (_ for _ in ()).throw(ContentCryptoError("invalid")))
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route=route, case="self_thought" if route == "ask" else "self_preparation_action")))
    result = await chat.post_message(OpeningSession(opening), UID, req, "guard-bad-opening")
    assert result.reply.kind == ("safety_check" if route == "ask" else "safety")
    chat.llm.generate.assert_not_awaited()


async def test_pressure_recovery_counts_opening_as_mandatory(setup, monkeypatch):
    normal_context(monkeypatch)
    monkeypatch.setattr(chat, "_context", AsyncMock(side_effect=chat._HistoryReadPressure))
    opening = row()
    req = request("나도")
    req.greeting_id = str(opening.id)
    checks = []

    def essential(system, messages, **kwargs):
        checks.append(messages)
        assert messages[-2] == {"role": "assistant", "content": opening.content}
        assert "나도" in messages[-1]["content"]
        return True

    monkeypatch.setattr(chat.context_retention, "fits_input_budget", essential)
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(OpeningSession(opening), UID, req, "opening-pressure")
    assert caught.value.code == "AI_UNAVAILABLE" and len(checks) == 1
    chat.llm.generate.assert_not_awaited()
    chat.checkpoint_repo.request_pressure.assert_awaited_once()
