"""Chat orchestration contracts; real publication constraints have local PG tests."""

import hashlib
import json
import uuid
from datetime import date

import pytest
from pydantic import ValidationError

from app.models.message import Message
from app.schemas.chat import PostMessageRequest
from app.services import chat, chat_turns, gating, llm, topic_chat
from app.services.llm import LLMResult
from app.services.topic_chat import TopicChatSnapshot
from tests.test_chat import FakeSession, UID, _gating


def test_legacy_digest_is_preserved_and_new_fields_are_bound():
    legacy = {"text": "yes", "greeting_id": None, "diary_references": False, "context_ref": None}
    expected = hashlib.sha256(json.dumps(
        legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    base = dict(text_value="yes", greeting_id=None)
    assert chat_turns.request_hash(**base) == expected
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert len({expected, chat_turns.request_hash(**base, topic_entry_id=a, locale="ko"),
                chat_turns.request_hash(**base, topic_entry_id=b, locale="ko"),
                chat_turns.request_hash(**base, topic_entry_id=a, locale="ja"),
                chat_turns.request_hash(**base, locale="en")}) == 5


@pytest.mark.parametrize("extra", [
    {}, {"locale": "fr"}, {"locale": "ko", "greeting_id": "existing"},
    {"locale": "ko", "context_ref": {"type": "daily_fortune", "local_date": "2026-09-07", "locale": "ko"}},
])
def test_first_topic_request_rejects_ambiguous_context(extra):
    with pytest.raises(ValidationError):
        PostMessageRequest.model_validate({"text": "yes", "topic_entry_id": str(uuid.uuid4()), **extra})


@pytest.mark.parametrize("crisis", [False, True])
@pytest.mark.parametrize("user_text", ["少し散歩したよ", "明日の面接が不安なんだ"])
async def test_first_question_is_one_turn_context_and_publication_is_tied_to_user_message(monkeypatch, crisis, user_text):
    entry_id = uuid.uuid4()
    snapshot = TopicChatSnapshot(entry_id, "今日、ちょっとうれしいことはあった？", date(2026, 9, 7), 0)
    seen = {}

    async def load(*args, **kwargs):
        seen["load"] = kwargs
        return snapshot

    async def publish(session, **kwargs):
        seen["publish"] = kwargs
        # The user row already exists, and final response persistence is still pending.
        assert any(isinstance(m, Message) and m.id == kwargs["user_message_id"] for m in session.added)

    async def resolve(*args, **kwargs):
        return _gating()

    async def generate(system, convo, **kwargs):
        seen["system"], seen["convo"] = system, convo
        return LLMResult(text="そうだったんだね。", input_tokens=10, output_tokens=20)

    monkeypatch.setattr(topic_chat, "load_snapshot", load)
    monkeypatch.setattr(topic_chat, "publish", publish)
    monkeypatch.setattr(gating, "resolve", resolve)
    monkeypatch.setattr(llm, "generate", generate)
    monkeypatch.setattr(chat.context_safety, "is_continuing_distress", lambda *a: crisis)
    session = FakeSession()
    req = PostMessageRequest(text=user_text, topic_entry_id=entry_id, locale="ja")
    response = await chat.post_message(session, UID, req, "topic-first")
    assert seen["convo"][-1]["role"] == "user"
    assert seen["convo"][-1]["content"].endswith("\n" + user_text)
    assert seen["load"]["locale"] == "ja"
    assert seen["publish"]["snapshot"] == snapshot
    messages = [m for m in session.added if isinstance(m, Message)]
    if crisis:
        assert response.greeting is None
        assert snapshot.question not in str(seen["convo"])
        assert seen["publish"]["greeting_message_id"] is None
    else:
        assert response.greeting.content == snapshot.question
        assert messages[0].kind == "topic_opening"
        assert messages[0].content == snapshot.question
        assert messages[0].id == seen["publish"]["greeting_message_id"]
        assert snapshot.question in seen["convo"][-2]["content"]
        assert "2026-09-07" in seen["convo"][-2]["content"]
        assert snapshot.question not in str(seen["system"])
    assert all(m.kind == "normal" for m in messages if m.kind != "topic_opening")


async def test_followup_does_not_reload_topic_context(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("ordinary followup must not load a topic snapshot")

    async def resolve(*args, **kwargs):
        return _gating()

    async def generate(system, convo, **kwargs):
        assert "배너에서 이미 건넨 질문" not in str(convo)
        return LLMResult(text="그랬구나.", input_tokens=10, output_tokens=20)

    monkeypatch.setattr(topic_chat, "load_snapshot", forbidden)
    monkeypatch.setattr(gating, "resolve", resolve)
    monkeypatch.setattr(llm, "generate", generate)
    result = await chat.post_message(FakeSession(), UID, PostMessageRequest(text="응", locale="ko"), "followup")
    assert result.greeting is None
