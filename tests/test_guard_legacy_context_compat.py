"""Guard-only activation keeps legacy readers and completed-crisis projection.

Public chat flow with synthetic DB/provider doubles; no DB or model dispatch.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import chat, checkpoint, crisis_chat, crisis_guard, llm
from tests.test_chat import UID
from tests.test_guard_context_v2_integration_review import Session, request, setup as setup
from tests.test_guard_input_isolation import Rows, row

ACTUAL_RECENT = chat._guard_recent


CASES = [
    ("ko", "지금 죽고 싶어.", "지금 안전한 곳이야? 혼자 있지 말고 주변 사람에게 알려줘.",
     "다른 얘기하자. 오늘 영화 봤어.", "오늘 영화의 주인공이 정말 좋았어.",
     "그 영화의 마지막 장면이 인상적이었어.", "예전에 새 프로젝트를 시작했다."),
    ("en", "I want to die.", "Are you safe right now? Please contact a trusted person nearby.",
     "By the way, I watched a movie today.", "I really liked the main character in the movie.",
     "The final scene of that movie was memorable.", "Previously started a new project."),
    ("ja", "死にたい。", "今は安全？一人でいないで近くの人に知らせて。",
     "ところで、今日は映画を観た。", "映画の主人公がとても良かった。",
     "その映画の最後の場面が印象的だった。", "以前、新しいプロジェクトを始めた。"),
]


@pytest.mark.parametrize("guard_enabled", [True, False], ids=["guard-on", "guard-off"])
@pytest.mark.parametrize("language,crisis,safety,shift,neutral,current,summary_fact", CASES,
                         ids=[case[0] for case in CASES])
async def test_guard_only_keeps_legacy_context_without_completed_crisis(
    setup, monkeypatch, guard_enabled, language, crisis, safety, shift, neutral, current, summary_fact,
):
    gate, configuration = setup
    configuration.update(crisis_guard_enabled=guard_enabled,
                         context_retention_v2_enabled=False,
                         context_retention_active_preparation=None)
    gate.profile.language = language
    monkeypatch.setattr(chat.settings, "context_checkpoint_enabled", True)
    monkeypatch.setattr(chat.settings, "context_retention_v2_enabled", False)
    original = [row(101, "user", "normal", "SYNTHETIC_RAW_FACT"),
                row(102, "moly", "normal", "SYNTHETIC_RAW_REPLY"),
                row(103, "user", "normal", crisis), row(104, "moly", "normal", safety),
                row(105, "user", "normal", shift), row(106, "moly", "normal", "SYNTHETIC_MOVIE_REPLY"),
                row(107, "user", "normal", neutral), row(108, "moly", "normal", "SYNTHETIC_LAST_REPLY")]
    stored_before = [(item.id, item.kind, item.content) for item in original]
    real_context = chat._context

    async def context(session, uid, anchor, **kwargs):
        return await real_context(Rows(original), uid, anchor, **kwargs)

    async def recent(session, uid, limit):
        assert limit == 6
        return await ACTUAL_RECENT(Rows(original[-6:]), uid, limit)

    monkeypatch.setattr(chat, "_context", AsyncMock(side_effect=context))
    monkeypatch.setattr(chat, "_guard_recent", AsyncMock(side_effect=recent))
    decide = AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="normal", case="not_applicable"))
    monkeypatch.setattr(crisis_chat, "recorded_decision", decide)
    chat.checkpoint_repo.load_latest.return_value = checkpoint.Checkpoint(
        uuid.UUID(int=91), 100, "\n".join((summary_fact, crisis, safety)),
        checkpoint.LEGACY_SUMMARIZER_VERSION, "a" * 64)
    chat.contract_repo.published_text.return_value = "SYNTHETIC_LEGACY_PREFERENCE"
    chat._recall_memory_v2.return_value = "SYNTHETIC_EXISTING_MEMORY"
    chat.relationship_projector.prompt_text.return_value = "SYNTHETIC_EXISTING_RELATIONSHIP"
    chat.llm.generate.return_value = llm.LLMResult(
        {"ko": "마지막 장면이 기억에 남았구나.", "en": "The last scene stayed with you.",
         "ja": "最後の場面が心に残ったんだね。"}[language], 10, 20, finish_reason="stop")

    class LegacySession(Session):
        async def execute(self, statement, params=None):
            if "SELECT key,value FROM app_config" in str(statement):
                return SimpleNamespace(all=lambda: list(configuration.items()))
            return await super().execute(statement, params)

    await chat.post_message(LegacySession(), UID, request(current), "guard-legacy-compat")

    chat.llm.generate.assert_awaited_once()
    system, messages = chat.llm.generate.await_args.args[:2]
    wire = "\n".join([*system, *(entry["content"] for entry in messages)])
    # Check the actual prompt rather than the flags passed to the assembler.
    assert crisis not in wire, "completed legacy raw/summary crisis leaked into generation"
    assert safety not in wire, "completed legacy safety response leaked into generation"
    for expected in (summary_fact, "SYNTHETIC_RAW_FACT", "SYNTHETIC_RAW_REPLY", shift, neutral,
                     "SYNTHETIC_LEGACY_PREFERENCE", "SYNTHETIC_EXISTING_MEMORY"):
        assert expected in wire
    assert messages[-1]["role"] == "user" and messages[-1]["content"].endswith(current)
    assert [(item.id, item.kind, item.content) for item in original] == stored_before
    chat.context_retention.load_required_facts.assert_not_awaited()
    chat.context_retention.pending_correction.assert_not_awaited()
    chat.checkpoint_repo.request_pressure.assert_not_awaited()
    assert chat._context.await_args.kwargs["retention_enabled"] is False
    assert chat.checkpoint_repo.load_latest.await_args.kwargs.get("retention_enabled", False) is False
    assert chat.contract_repo.published_text.await_args.kwargs.get("retention_enabled", False) is False
    assert chat._recall_memory_v2.await_args.kwargs.get("retention_enabled", False) is False
    chat.relationship_projector.prompt_text.assert_awaited_once()
    assert decide.await_count == int(guard_enabled)
    if guard_enabled:
        # The independent classifier still sees six exact original rows, including
        # the legacy crisis. Projection never changes its judgment input or proof.
        passed = decide.await_args.args[1]
        assert [(item.message_id, item.kind, item.content) for item in passed] == stored_before[-6:]
        assert all(isinstance(item, crisis_guard.GuardMessage) for item in passed)
        assert crisis in [item.content for item in passed]


@pytest.mark.parametrize("language,crisis,safety,shift,neutral,current,summary_fact", CASES,
                         ids=[case[0] for case in CASES])
async def test_current_crisis_stays_exact_in_legacy_projection(
    monkeypatch, language, crisis, safety, shift, neutral, current, summary_fact,
):
    original = [row(1, "user", "normal", crisis), row(2, "moly", "normal", safety),
                row(3, "user", "normal", shift), row(4, "moly", "normal", "SYNTHETIC_REPLY")]
    result = await chat._context(Rows(original), chat._uid(UID), 0, current_text=crisis,
        language=language, retention_enabled=False, compact_crises=True,
        normal_generation_projection=True)
    assert result.conversation[-1]["content"].endswith(crisis)
    assert sum(crisis in item["content"] for item in result.conversation) == 2
