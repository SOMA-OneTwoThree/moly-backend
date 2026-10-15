"""Selected reply contracts with synthetic input; no claim of generated tone quality."""
from types import SimpleNamespace
import uuid

import pytest

from app.services import chat, gating, llm, mem0_recall, prompts
from tests.test_chat import UID, FakeSession, _gating
from tests.test_mem0_recall import _Adapter, _Session, _embed, _Hit


@pytest.mark.parametrize("environment", ["production", "development", "local"])
@pytest.mark.parametrize("locale", ["ko", "ko-KR", "ja", "en"])
def test_normal_path_uses_static_locale_persona_and_preserves_model_punctuation(monkeypatch, environment, locale):
    monkeypatch.setattr(chat.settings, "environment", environment)
    first = chat._build_system(locale, "Alex")
    second = chat._build_system(locale, "Nari")
    assert first[0] == second[0] == prompts.system_prompt(locale)
    assert first[1] != second[1]
    text = '"무슨 일이야."\n어디든 갈 수 있어.'
    assert chat._clean_reply(text, language=locale) == text


@pytest.mark.parametrize("locale", ["ko", "ja", "en"])
def test_removed_forced_emotion_closeness_and_slow_persona_instructions(locale):
    prompt = prompts.system_prompt(locale)
    for old in ("마음이 안 보이면 실패", "수줍게 거리를", "느긋한", "여운", "気持ちが伝わらないと失敗", "はにかんで", "のんびり", "余韻", "If your heart doesn't show", "Taking life slowly", "a little distance"):
        assert old not in prompt


@pytest.mark.parametrize("locale,header", [("ko", "[상대]"), ("ja", "[相手]"), ("en", "[Conversation partner]")])
def test_mutable_partner_is_localized_and_relationship_stage_does_not_force_korean_tone(locale, header):
    blocks = chat._build_system(locale, "Alex", relationship_v2="[관계]\n함께한 날: 2일\n낯선 상대에겐 수줍게")
    assert header in blocks[1]
    if locale == "ko":
        assert "함께한 날: 2일" in blocks[1]
        assert "수줍게" not in blocks[1]


async def test_normal_reply_preserves_quoted_foreign_text_lines_and_statements(monkeypatch):
    raw = '"I really like that..."\n어디든 갈 수 있어. 猫'

    async def resolve(*args, **kwargs):
        return _gating()

    async def generate(*args, **kwargs):
        return llm.LLMResult(text=raw, finish_reason="stop", input_tokens=3, output_tokens=4)

    monkeypatch.setattr(gating, "resolve", resolve)
    monkeypatch.setattr(llm, "generate", generate)
    out = await chat.post_message(FakeSession(), UID, SimpleNamespace(text="이 문구 인용해 줘", greeting_id=None), "quote-release")
    assert out.reply.content == raw


@pytest.mark.parametrize("mode", ["empty", "skipped", "embedding", "provider", "registry"])
async def test_recall_availability_distinguishes_failure_from_empty_without_changing_ranking(mode):
    async def embed(query, **kwargs):
        if mode == "embedding":
            raise TimeoutError("synthetic")
        return await _embed(query, **kwargs)

    status = {}
    trace = {}
    hits = [_Hit("id1", 0.2, {"memory": "something"})] if mode == "registry" else []
    result = await mem0_recall.recall(
        _Session([], boom=mode == "registry"), uuid.UUID(UID),
        query="hi" if mode == "skipped" else "전에 했던 회사 이야기 기억나?",
        adapter=_Adapter(hits, boom=mode == "provider"), embed_query=embed,
        availability=status, trace=trace,
    )
    assert result == []
    assert status["status"] == ("skipped" if mode == "skipped" else "ok" if mode == "empty" else "unavailable")
    if mode != "skipped":
        assert trace
