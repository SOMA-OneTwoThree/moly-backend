"""Development Korean removes persisted stage tone instructions at delivery only."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import chat, naming, relationship_projector as projector
from tests.test_chat import FakeSession
from tests.test_chat_turn_context_wiring import _post


_CACHED_STAGE_LINES = {
    "new": "아직 알아가는 중이야. 조심스럽게 다가가.",
    "acquainted": "이제 서로 조금은 아는 사이야. 너무 어색해하지 않아도 돼.",
    "familiar": "이제 제법 익숙한 사이야. 편하게 말해도 돼.",
    "close": "오래 함께한 사이야. 굳이 설명하지 않아도 통하는 게 있어.",
}


@pytest.mark.parametrize("stage", _CACHED_STAGE_LINES)
@pytest.mark.parametrize("locale", ["ko", "ko-KR"])
def test_development_korean_strips_all_cached_stages_preserving_other_stable_values(
    monkeypatch, stage, locale,
):
    monkeypatch.setattr(chat.settings, "environment", "development")
    cached = "[관계]\n함께한 날: 12일\n" + _CACHED_STAGE_LINES[stage]
    lead, past, contract = "인사 {유저이름}", "2026-10-14T01:00:00+09:00 원문", "합의 {유저이름}"
    blocks = chat._build_system(locale, "지훈", [lead], historical_lead=[past],
                                contract_text=contract, relationship_v2=cached)
    assert blocks[0] == chat.eval_system_prompt("ko", "voice-structured-ko-v9")[0]
    assert blocks[1].endswith("[관계]\n함께한 날: 12일")
    assert cached.splitlines()[-1] not in "\n".join(blocks)
    assert naming.render(lead, "지훈") in blocks[1]
    assert past in blocks[1] and naming.render(contract, "지훈") in blocks[1]
    assert "지훈이야" in blocks[1]
    # Persistence still generates its original full cache, including stage text.
    assert projector.render_state(stage=stage, active_days=12, language=locale) == cached
    assert projector.RENDERER_VERSION == "relationship-render-v1"


@pytest.mark.parametrize("environment,locale", [
    (env, locale) for env in ("production", "local", "test")
    for locale in ("ko", "ko-KR", "ja", "en")
] + [("development", "ja"), ("development", "ja-JP"),
     ("development", "en"), ("development", "en-US")])
def test_other_environments_and_locales_keep_cached_render_bytes(monkeypatch, environment, locale):
    monkeypatch.setattr(chat.settings, "environment", environment)
    cached = projector.render_state(stage="new", active_days=12, language=locale)
    with_relation = chat._build_system(locale, "Alex", relationship_v2=cached)
    without_relation = chat._build_system(locale, "Alex")
    assert with_relation[0] == without_relation[0]
    assert with_relation[1] == without_relation[1] + "\n\n" + cached


@pytest.mark.parametrize("cached", [
    "", "[관계]", "[관계]\n함께한 날: -1일\n조심스럽게 다가가.",
    "[관계]\n함께한 날: 01일\n조심스럽게 다가가.",
    "[관계]\n함께한 날: １２일\n조심스럽게 다가가.",
    "[관계]\n함께한 날: 12일\n함께한 날: 13일\n추가 지시",
    "[관계]\n함께한 날: 12일\n함께한 날: 13일",
    "[관계]\n함께한 날: 12일\n[관계]",
    "[관계]\n함께한 날: 12일\n단계\n추가 지시",
    "[관계]\n함께한 날: 12일; 추가 지시", "[다른 헤더]\n함께한 날: 12일",
])
def test_malformed_or_ambiguous_cache_never_falls_back_to_its_instructions(monkeypatch, cached):
    monkeypatch.setattr(chat.settings, "environment", "development")
    assert projector.relationship_facts(cached) == ""
    assert chat._build_system("ko", "Alex", relationship_v2=cached) == chat._build_system("ko", "Alex")


def test_canonical_fact_only_cache_and_unknown_third_line_preserve_only_days():
    for days in (0, 3, 120):
        facts = f"[관계]\n함께한 날: {days}일"
        assert projector.relationship_facts(facts) == facts
        assert projector.relationship_facts(facts + "\n임의의 오래된 말투 지시") == facts


async def test_cached_render_reader_stays_read_only_and_returns_original_bytes():
    cached = "[관계]\n함께한 날: 3일\n" + _CACHED_STAGE_LINES["new"]
    session = SimpleNamespace(scalar=AsyncMock(return_value=cached), execute=AsyncMock())
    assert await projector.prompt_text(session, uuid.uuid4(), language="ko") == cached
    session.scalar.assert_awaited_once()
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("stage", _CACHED_STAGE_LINES)
async def test_real_post_message_projects_cached_stage_before_llm_without_mutating_cache(
    monkeypatch, stage,
):
    from app.services import contract_repo, memory_pipeline

    monkeypatch.setattr(chat.settings, "environment", "development")
    monkeypatch.setattr(memory_pipeline, "load", AsyncMock(return_value=SimpleNamespace(serves_v2=True)))
    monkeypatch.setattr(chat, "_recall_memory_v2", AsyncMock(return_value=""))
    monkeypatch.setattr(chat, "_record_memory_v2", AsyncMock())
    monkeypatch.setattr(contract_repo, "published_text", AsyncMock(return_value="합의 {유저이름}"))
    cached = "[관계]\n함께한 날: 7일\n" + _CACHED_STAGE_LINES[stage]
    reader = AsyncMock(return_value=cached)
    monkeypatch.setattr(projector, "prompt_text", reader)
    capture = {}
    await _post(FakeSession(), monkeypatch, capture=capture)
    reader.assert_awaited_once()
    assert reader.return_value == cached
    assert capture["system"][1].endswith("[관계]\n함께한 날: 7일")
    assert "합의 지훈" in capture["system"][1] and "지훈이야" in capture["system"][1]
    assert _CACHED_STAGE_LINES[stage] not in "\n".join(capture["system"])
    assert all(_CACHED_STAGE_LINES[stage] not in entry["content"] for entry in capture["convo"])
    assert capture["convo"][-1]["role"] == "user"
