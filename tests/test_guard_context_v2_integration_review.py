"""Independent integration regression checks; fake DB/provider only.

These exercise the public chat service, including the publish/quota boundary.
They do not claim real model accuracy or existing development API validation.
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import errors
from app.models.idempotency_key import IdempotencyKey
from app.models.message import Message
from app.services import chat, crisis_chat, crisis_guard, llm
from app.services.context_provenance import HistoryEntry, SourceRef, VerifiedCoverage
import hashlib
import uuid
from tests.test_chat import FakeSession, UID, _gating

ACTUAL_ASSERT_BUDGET = chat.context_retention.assert_input_budget
ACTUAL_FITS_BUDGET = chat.context_retention.fits_input_budget


class Session(FakeSession):
    def __init__(self):
        super().__init__()
        self.commits = 0

    async def commit(self):
        self.commits += 1
        await super().commit()


@pytest.fixture
def setup(monkeypatch):
    configuration = {
        "crisis_guard_enabled": True, "crisis_guard_policy_version": "crisis-v2",
        "context_retention_v2_enabled": True,
    }
    monkeypatch.setattr(chat.config_store, "get_config_values", AsyncMock(return_value=configuration))
    gate = _gating()
    monkeypatch.setattr(chat.gating, "resolve", AsyncMock(return_value=gate))
    monkeypatch.setattr(chat, "_guard_recent", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat, "_accumulate_tokens", AsyncMock(return_value=gate.tokens_used))
    monkeypatch.setattr(chat.chat_turns, "release", AsyncMock())
    monkeypatch.setattr(chat.memory_pipeline, "load", AsyncMock(return_value=SimpleNamespace(serves_v2=True)))
    monkeypatch.setattr(chat.relationship_projector, "prompt_text", AsyncMock(return_value=""))
    monkeypatch.setattr(chat.contract_repo, "published_text", AsyncMock(return_value=""))
    monkeypatch.setattr(chat.context_retention, "pending_correction", AsyncMock(return_value=False))
    monkeypatch.setattr(chat.context_retention, "load_required_facts", AsyncMock(return_value=""))
    monkeypatch.setattr(chat.context_retention, "enabled_now", AsyncMock(return_value=True))
    monkeypatch.setattr(chat.context_retention, "assert_input_budget", lambda *a, **kw: None)
    monkeypatch.setattr(chat.context_retention, "fits_input_budget", lambda *a, **kw: True)
    monkeypatch.setattr(chat.checkpoint_repo, "load_latest", AsyncMock(return_value=None))
    monkeypatch.setattr(chat.checkpoint_repo, "request_pressure", AsyncMock())
    monkeypatch.setattr(chat, "_recall_memory_v2", AsyncMock(return_value=""))
    monkeypatch.setattr(chat, "_record_memory_v2", AsyncMock())
    monkeypatch.setattr(chat.llm, "generate", AsyncMock(return_value=llm.LLMResult(
        "합성 답변이야.", 10, 20, finish_reason="stop")))
    return gate, configuration


def request(text="합성 검증 입력", **kwargs):
    return SimpleNamespace(text=text, greeting_id=None, **kwargs)


@pytest.mark.parametrize("language,reply", [
    ("ko", "오늘 하늘이 맑구나."), ("en", "The sky is clear today."), ("ja", "今日は晴れているんだね。"),
])
@pytest.mark.parametrize("intervention", ["safety", "safety_check"])
async def test_normal_after_intervention_keeps_history_and_uses_only_ephemeral_guidance(
    setup, monkeypatch, language, reply, intervention,
):
    gate, _ = setup
    gate.profile.language = language
    prior_user, prior_reply, current = "synthetic earlier user subject", "synthetic earlier fixed reply", "synthetic current weather"
    recent = [crisis_guard.GuardMessage("user", prior_user, kind=intervention, message_id=1),
              crisis_guard.GuardMessage("assistant", prior_reply, kind=intervention, message_id=2)]
    chat._guard_recent.return_value = recent
    monkeypatch.setattr(chat, "_context", AsyncMock(return_value=chat.ContextWindow(
        conversation=[{"role": "user", "content": prior_user},
                      {"role": "assistant", "content": prior_reply},
                      {"role": "user", "content": current}],
        new_anchor=None, lead=[], checkpoint_keep_from=None, incomplete=False,
    )))
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="normal", case="not_applicable", last_intervention=intervention)))
    chat.llm.generate.return_value = llm.LLMResult(reply, 10, 20, finish_reason="stop")
    session = Session()
    await chat.post_message(session, UID, request(current), "normal-after-intervention")
    chat.llm.generate.assert_awaited_once()
    system, messages = chat.llm.generate.await_args.args[:2]
    instruction = crisis_guard.normal_reply_instruction(language)
    assert instruction not in "\n".join(system)
    assert sum(instruction in message["content"] for message in messages) == 1
    assert messages[-2]["role"] == "system" and instruction in messages[-2]["content"]
    assert messages[-1] == {"role": "user", "content": current}
    assert any(message["content"] == prior_user for message in messages)
    assert any(message["content"] == prior_reply for message in messages)
    assert all(instruction not in item.content and item.kind == "normal"
               for item in session.added if isinstance(item, Message))
    assert chat._accumulate_tokens.await_args.args[-1] > 0


@pytest.mark.parametrize("status,intervention", [
    ("ok", None), ("timeout", "safety"), ("error", "safety"), ("refusal", "safety"),
    ("invalid", "safety_check"), ("conflict", "safety_check"),
])
async def test_normal_guidance_does_not_restrict_fault_fallback_or_no_intervention(
    setup, monkeypatch, status, intervention,
):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        status, route="normal", case="not_applicable" if status == "ok" else None,
        last_intervention=intervention)))
    await chat.post_message(Session(), UID, request(), "normal-without-instruction")
    system, messages = chat.llm.generate.await_args.args[:2]
    instruction = crisis_guard.normal_reply_instruction("ko")
    assert instruction not in "\n".join(system)
    assert all(instruction not in message["content"] for message in messages)


@pytest.mark.parametrize("boundary", ["raw", "assembled"])
async def test_history_recovery_budget_includes_the_same_normal_reply_guidance(setup, monkeypatch, boundary):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="normal", case="not_applicable", last_intervention="safety")))
    instruction = crisis_guard.normal_reply_instruction("ko")
    checked = []

    def essential_budget(system, messages, **kwargs):
        checked.append(messages)
        assert sum(instruction in message["content"] for message in messages) == 1
        return True

    monkeypatch.setattr(chat.context_retention, "fits_input_budget", essential_budget)
    if boundary == "raw":
        monkeypatch.setattr(chat, "_context", AsyncMock(side_effect=chat._HistoryReadPressure))
    else:
        def assembled_budget(system, messages, **kwargs):
            assert sum(instruction in message["content"] for message in messages) == 1
            raise chat.context_retention.RetentionError("context_input_budget")
        monkeypatch.setattr(chat.context_retention, "assert_input_budget", assembled_budget)
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(Session(), UID, request(), "guidance-budget")
    assert caught.value.code == "AI_UNAVAILABLE" and len(checked) == 1
    chat.llm.generate.assert_not_awaited()
    chat.checkpoint_repo.request_pressure.assert_awaited_once()


@pytest.mark.parametrize("language", ["ko", "en", "ja"])
@pytest.mark.parametrize("usage_v2", [False, True])
async def test_fixed_guidance_bypasses_all_normal_state_and_charges_zero(setup, monkeypatch, language, usage_v2):
    gate, _ = setup
    gate.profile.language = language
    monkeypatch.setattr(chat.settings, "turn_usage_v2_enabled", usage_v2)
    decision = AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="safety", case="self_preparation_action"))
    monkeypatch.setattr(crisis_chat, "recorded_decision", decision)
    normal_dependencies = [chat.memory_pipeline.load, chat.context_retention.pending_correction,
                           chat.contract_repo.published_text, chat.context_retention.load_required_facts,
                           chat._recall_memory_v2, chat.llm.generate, chat._record_memory_v2]
    for dependency in normal_dependencies:
        dependency.side_effect = RuntimeError("synthetic normal state unavailable")
    session = Session()
    result = await chat.post_message(session, UID, request(), "fixed-test")
    assert result.reply.content == crisis_guard.fixed_reply(language)
    assert result.greeting is None and result.reply.references == [] and not result.review_prompt
    assert result.tokens_used == gate.tokens_used
    assert decision.await_count == 1
    assert all(dependency.await_count == 0 for dependency in normal_dependencies)
    assert chat._accumulate_tokens.await_args.args[-1] == 0
    messages = [item for item in session.added if isinstance(item, Message)]
    assert len(messages) == 2 and {m.sender for m in messages} == {"user", "moly"}
    assert all(m.kind == "safety" for m in messages)
    assert next(m for m in messages if m.sender == "moly").billable_tokens == 0
    assert session.commits == 2  # Common proof/lease, then atomic fixed publish.


@pytest.mark.parametrize("case,method,expected", [
    ("self_thought", False, "어떤 뜻인지 조금만 더 얘기해줄래?"),
    ("meaning_unclear", False, "어떤 뜻인지 조금만 더 얘기해줄래?"),
    ("self_thought", True, "그건 알려 줄 수 없어. 어떤 뜻인지 조금만 더 얘기해줄래?"),
    ("meaning_unclear", None, "그건 알려 줄 수 없어. 어떤 뜻인지 조금만 더 얘기해줄래?"),
])
async def test_check_is_the_selected_fixed_question_without_generation(setup, monkeypatch, case, method, expected):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="ask", case=case, method_request=method)))
    result = await chat.post_message(Session(), UID, request(), "ask-test")
    assert result.reply.content == expected
    chat.llm.generate.assert_not_awaited()
    chat.memory_pipeline.load.assert_not_awaited()
    assert chat._accumulate_tokens.await_args.args[-1] == 0


async def test_decision_starts_after_common_commit_and_before_any_recall(setup, monkeypatch):
    session = Session()
    events = []

    async def decide(*args, **kwargs):
        assert session.commits == 1
        events.append("decision")
        return crisis_guard.DecisionResult("ok", case="not_applicable")

    async def recall(*args, **kwargs):
        events.append("recall")
        return ""

    async def generate(*args, **kwargs):
        events.append("generate")
        return llm.LLMResult("합성 정상 답변.", 10, 20, finish_reason="stop")

    monkeypatch.setattr(crisis_chat, "recorded_decision", decide)
    monkeypatch.setattr(chat, "_recall_memory_v2", recall)
    monkeypatch.setattr(chat.llm, "generate", generate)
    result = await chat.post_message(session, UID, request(), "normal-test")
    assert result.reply.content == "합성 정상 답변."
    assert events == ["decision", "recall", "generate"]
    assert chat._accumulate_tokens.await_args.args[-1] > 0
    assert chat._record_memory_v2.await_args.kwargs["record_derived"] is True


async def test_unused_required_fact_revision_never_blocks_fixed_guidance(setup, monkeypatch):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="safety", case="self_intent_plan")))
    chat.context_retention.load_required_facts.side_effect = ["old synthetic fact", "new synthetic fact"]
    result = await chat.post_message(Session(), UID, request(), "unused-revision")
    assert result.reply.content == crisis_guard.fixed_reply("ko")
    chat.context_retention.load_required_facts.assert_not_awaited()


@pytest.mark.parametrize("changed", ["content", "role", "kind", "message_id"])
async def test_changed_recent_source_prevents_fixed_publish(setup, monkeypatch, changed):
    original = dict(role="user", content="합성 과거 원문", kind="normal", message_id=3)
    modified = {**original, changed: {"content": "수정된 합성 원문", "role": "assistant",
                                    "kind": "safety", "message_id": 4}[changed]}
    chat._guard_recent.side_effect = [[crisis_guard.GuardMessage(**original)],
                                     [crisis_guard.GuardMessage(**modified)]]
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="safety", case="self_intent_plan")))
    session = Session()
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(session, UID, request(), "changed-source")
    assert caught.value.code == "CHAT_TURN_STALE"
    assert session.added == []
    chat._accumulate_tokens.assert_not_awaited()
    chat.chat_turns.release.assert_awaited_once()


async def test_privacy_or_generation_fence_still_stops_fixed_reply(setup, monkeypatch):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route="safety", case="self_intent_plan")))
    monkeypatch.setattr(chat.chat_turns, "verify_publish", AsyncMock(side_effect=errors.AppError(
        "CHAT_TURN_STALE", 409, "synthetic generation fence")))
    session = Session()
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(session, UID, request(), "changed-generation")
    assert caught.value.code == "CHAT_TURN_STALE" and session.added == []
    chat.chat_turns.release.assert_awaited_once()


async def test_same_request_replays_without_new_decision_or_quota(setup, monkeypatch):
    decide = AsyncMock(return_value=crisis_guard.DecisionResult("ok", route="safety", case="self_intent_plan"))
    monkeypatch.setattr(crisis_chat, "recorded_decision", decide)
    session = Session()
    first = await chat.post_message(session, UID, request(), "same-request")
    cached = next(item for item in session.added if isinstance(item, IdempotencyKey))
    session.get_map["IdempotencyKey"] = cached
    second = await chat.post_message(session, UID, request(), "same-request")
    assert first == second
    assert decide.await_count == 1 and chat._accumulate_tokens.await_count == 1


async def test_cancelled_classification_releases_only_owned_lease(setup, monkeypatch):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(side_effect=asyncio.CancelledError))
    session = Session()
    with pytest.raises(asyncio.CancelledError):
        await chat.post_message(session, UID, request(), "cancelled")
    assert session.added == []
    chat.chat_turns.release.assert_awaited_once()
    assert chat.chat_turns.release.await_args.kwargs["user_id"] == chat._uid(UID)


async def test_pressure_pause_reserves_real_work_and_commits_without_current_turn(setup, monkeypatch):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))

    def over_budget(*args, **kwargs):
        raise chat.context_retention.RetentionError("synthetic completed history too large")

    monkeypatch.setattr(chat.context_retention, "assert_input_budget", over_budget)
    session = Session()
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(session, UID, request(), "pressure-pause")
    assert caught.value.code == "AI_UNAVAILABLE"
    assert session.added == [] and session.commits >= 3
    chat.checkpoint_repo.request_pressure.assert_awaited_once()
    assert chat.checkpoint_repo.request_pressure.await_args.kwargs == {
        "user_id": chat._uid(UID), "generation": 0,
    }
    chat._accumulate_tokens.assert_not_awaited()
    chat.llm.generate.assert_not_awaited()


async def test_irreducible_current_input_does_not_reserve_endless_pressure_work(setup, monkeypatch):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))

    def over_budget(*args, **kwargs):
        raise chat.context_retention.RetentionError("synthetic irreducible input")

    monkeypatch.setattr(chat.context_retention, "assert_input_budget", over_budget)
    monkeypatch.setattr(chat.context_retention, "fits_input_budget", lambda *a, **kw: False)
    session = Session()
    with pytest.raises(errors.AppError):
        await chat.post_message(session, UID, request(), "irreducible")
    chat.checkpoint_repo.request_pressure.assert_not_awaited()
    assert session.added == []


async def test_successful_turn_proactively_reserves_through_actual_reply(setup, monkeypatch):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))
    monkeypatch.setattr(chat.context_retention, "fits_input_budget", lambda *a, **kw: False)
    session = Session()
    result = await chat.post_message(session, UID, request(), "proactive")
    chat.checkpoint_repo.request_pressure.assert_awaited_once()
    assert chat.checkpoint_repo.request_pressure.await_args.kwargs["through_id"] == int(result.reply.message_id)


@pytest.mark.parametrize(("prior_bytes", "current", "expected"), [
    (0, "가" * 1400, True),  # A first long completed turn already needs preparation.
    (4050, "새 대화", True),
    (0, "짧은 대화", False),
])
async def test_soft_prefetch_starts_after_recording_the_complete_source(
    setup, monkeypatch, prior_bytes, current, expected,
):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))
    monkeypatch.setattr(chat, "_context", AsyncMock(return_value=chat.ContextWindow(
        [{"role": "user", "content": current}], None, [], retention_source_bytes=prior_bytes)))
    order = []
    chat._record_memory_v2.side_effect = lambda *a, **kw: order.append("source")
    chat.checkpoint_repo.request_pressure.side_effect = lambda *a, **kw: order.append("checkpoint")
    session = Session()
    result = await chat.post_message(session, UID, request(current), "early-preparation")
    assert order == (["source", "checkpoint"] if expected else ["source"])
    if expected:
        assert chat.checkpoint_repo.request_pressure.await_args.kwargs["through_id"] == int(result.reply.message_id)
    assert chat.context_retention.fits_input_budget(None, []) is True


async def test_covered_tail_budget_fallback_keeps_uncovered_and_required_input(setup, monkeypatch):
    covered, uncovered, current = "covered" * 6000, "uncovered exact dialogue", "current dialogue"
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))
    uid = chat._uid(UID)
    refs = [SourceRef(uid, i, 0, hashlib.sha256(text.encode()).hexdigest())
            for i, text in enumerate((covered, "covered reply"), start=1)]
    coverage = VerifiedCoverage(uid, 0, uuid.UUID(int=9), 2, "a" * 64).bind_sources(refs)
    monkeypatch.setattr(chat, "_context", AsyncMock(return_value=chat.ContextWindow(
        [HistoryEntry("user", covered, source_ref=refs[0]),
         HistoryEntry("assistant", "covered reply", source_ref=refs[1]),
         {"role": "user", "content": uncovered}, {"role": "assistant", "content": "uncovered reply"},
         {"role": "user", "content": current}], None, [], coverage=coverage)))
    chat.context_retention.load_required_facts.return_value = "required stable fact"
    monkeypatch.setattr(chat.context_retention, "assert_input_budget", ACTUAL_ASSERT_BUDGET)
    monkeypatch.setattr(chat.context_retention, "fits_input_budget", ACTUAL_FITS_BUDGET)
    result = await chat.post_message(Session(), UID, request(current), "covered-tail-fallback")
    assert result.reply.content == "합성 답변이야."
    system, convo = chat.llm.generate.await_args.args[:2]
    encoded = str(system) + str(convo)
    assert covered not in encoded
    assert all(text in encoded for text in (uncovered, current, "required stable fact", "uncovered reply"))
    chat.checkpoint_repo.request_pressure.assert_not_awaited()


async def test_normal_required_fact_change_remains_a_publish_barrier(setup, monkeypatch):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))
    chat.context_retention.load_required_facts.side_effect = ["old synthetic fact", "new synthetic fact"]
    session = Session()
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(session, UID, request(), "normal-revision")
    assert caught.value.code == "AI_UNAVAILABLE" and session.added == []


async def test_leading_assistant_keeps_identity_outside_required_system(setup, monkeypatch):
    uid, text, current = chat._uid(UID), "covered lead " * 4000, "current synthetic question"
    ref = SourceRef(uid, 4, 0, hashlib.sha256(text.encode()).hexdigest())
    coverage = VerifiedCoverage(uid, 0, uuid.UUID(int=9), 4, "a" * 64).bind_sources([ref])
    lead = SimpleNamespace(id=4, sender="moly", content=text, created_at=None)
    monkeypatch.setattr(chat, "_context", AsyncMock(return_value=chat.ContextWindow(
        [{"role": "user", "content": "uncovered source"},
         {"role": "assistant", "content": "uncovered reply"},
         {"role": "user", "content": current}], None, [lead], coverage=coverage)))
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))
    monkeypatch.setattr(chat.context_retention, "assert_input_budget", ACTUAL_ASSERT_BUDGET)
    monkeypatch.setattr(chat.context_retention, "fits_input_budget", ACTUAL_FITS_BUDGET)
    await chat.post_message(Session(), UID, request(current), "leading-source")
    system, messages = chat.llm.generate.await_args.args[:2]
    assert text not in str(system) + str(messages)
    assert all(s in str(messages) for s in ("uncovered source", "uncovered reply", current))
    assert all(set(m) == {"role", "content"} for m in messages)


async def test_normal_revision_pause_can_retry_same_key_then_replay_without_double_charge(setup, monkeypatch):
    decision = AsyncMock(return_value=crisis_guard.DecisionResult("ok", case="not_applicable"))
    monkeypatch.setattr(crisis_chat, "recorded_decision", decision)
    facts = chat.context_retention.load_required_facts
    facts.side_effect = ["old synthetic fact", "new synthetic fact"]
    first = Session()
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(first, UID, request(), "same-normal-request")
    assert caught.value.code == "AI_UNAVAILABLE" and first.added == []
    chat._accumulate_tokens.assert_not_awaited()
    chat.chat_turns.release.assert_awaited_once()

    facts.side_effect = None
    facts.return_value = "new synthetic fact"
    retry_session = Session()
    accepted = await chat.post_message(retry_session, UID, request(), "same-normal-request")
    assert len([m for m in retry_session.added if isinstance(m, Message)]) == 2
    assert chat._accumulate_tokens.await_count == 1
    assert chat._accumulate_tokens.await_args.args[-1] > 0
    cached = next(row for row in retry_session.added if isinstance(row, IdempotencyKey))
    replay_session = Session()
    replay_session.get_map["IdempotencyKey"] = cached
    replay = await chat.post_message(replay_session, UID, request(), "same-normal-request")
    assert replay == accepted and replay_session.added == []
    assert decision.await_count == chat.llm.generate.await_count == 2
    assert chat._accumulate_tokens.await_count == 1


async def test_policy_version_gate_preserves_legacy_path(setup, monkeypatch):
    _, configuration = setup
    configuration["crisis_guard_policy_version"] = "old-policy"
    decide = AsyncMock()
    monkeypatch.setattr(crisis_chat, "recorded_decision", decide)
    result = await chat.post_message(Session(), UID, request(), "old-policy")
    assert result.reply.content == "합성 답변이야."
    decide.assert_not_awaited()


@pytest.mark.parametrize("route", ["ask", "safety", "normal"])
async def test_exhausted_quota_still_classifies_and_allows_only_zero_charge_fixed_replies(setup, monkeypatch, route):
    gate, _ = setup
    gate.entitlement["tokens_remaining"] = 0
    gate.tokens_used = gate.entitlement["daily_token_limit"]
    chat._accumulate_tokens.return_value = gate.tokens_used
    decision = AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route=route, case="self_thought" if route == "ask" else "self_intent_plan"))
    monkeypatch.setattr(crisis_chat, "recorded_decision", decision)
    session = Session()
    if route == "normal":
        with pytest.raises(errors.AppError) as caught:
            await chat.post_message(session, UID, request(), "depleted")
        assert caught.value.code == "DAILY_LIMIT_REACHED" and session.added == []
        chat._accumulate_tokens.assert_not_awaited()
    else:
        result = await chat.post_message(session, UID, request(), "depleted")
        assert result.tokens_used == gate.tokens_used and result.tokens_remaining == 0
        assert chat._accumulate_tokens.await_args.args[-1] == 0
    decision.assert_awaited_once()
    chat.memory_pipeline.load.assert_not_awaited()
    chat.llm.generate.assert_not_awaited()


def choice(name, value, values):
    return {"type": "choice", "name": name, "choice": value, "confidence": 0.9,
            "probabilities": [{"value": item, "probability": float(item == value)} for item in values]}


@pytest.mark.parametrize("case", ["not_applicable", "self_thought", "meaning_unclear",
                                 "self_intent_plan", "self_preparation_action"])
@pytest.mark.parametrize("intervention,answer", [
    (None, None), ("safety", None), ("safety_check", "same_confirmed"),
    ("safety_check", "same_denied"), ("safety_check", "same_unresolved"),
    ("safety_check", "different_topic"),
])
def test_independent_route_table_oracle(case, intervention, answer):
    # Enumerate policy independently, without choose_route or CASE constants.
    cases = ("not_applicable", "self_thought", "self_intent_plan",
             "self_preparation_action", "meaning_unclear")
    answers = ("same_confirmed", "same_denied", "same_unresolved", "different_topic")
    items = [choice("case", case, cases),
             {"type": "predicate", "name": "method_request", "probability": 0.1}]
    if intervention == "safety_check":
        items.append(choice("followup", answer, answers))
    if case in {"self_intent_plan", "self_preparation_action"}:
        expected = "safety"
    elif case == "not_applicable" or intervention == "safety":
        expected = "normal"
    elif intervention == "safety_check":
        expected = "safety" if answer == "same_confirmed" else "normal"
    else:
        expected = "ask"
    parsed = crisis_guard.parse_decision({"answers": items}, last_intervention=intervention)
    assert parsed.route == expected
    assert parsed.status == ("conflict" if case == "not_applicable" and answer == "same_confirmed" else "ok")


@pytest.mark.parametrize("auxiliary", ["refusal", "missing", "duplicate", "nonfinite"])
@pytest.mark.parametrize("case", ["self_intent_plan", "self_preparation_action"])
def test_independent_action_is_not_lowered_by_auxiliary_failure(case, auxiliary):
    cases = ("not_applicable", "self_thought", "self_intent_plan",
             "self_preparation_action", "meaning_unclear")
    items = [choice("case", case, cases)]
    if auxiliary == "refusal":
        items.extend([{"name": "method_request", "type": "refusal"},
                      {"name": "followup", "type": "refusal"}])
    elif auxiliary == "duplicate":
        items.extend([{"name": "method_request", "type": "predicate", "probability": 0.1}] * 2)
        items.extend([{"name": "followup", "type": "refusal"}] * 2)
    elif auxiliary == "nonfinite":
        items.append({"name": "method_request", "type": "predicate", "probability": float("nan")})
    parsed = crisis_guard.parse_decision({"answers": items}, last_intervention="safety_check")
    assert parsed.route == "safety" and parsed.case == case


@pytest.mark.parametrize("distance,expected", [(5, "safety_check"), (6, None), (7, None)])
def test_independent_six_message_boundary_is_messages_and_assistant_only(distance, expected):
    history = [crisis_guard.GuardMessage("assistant", "합성 질문", "safety_check", 1)]
    history += [crisis_guard.GuardMessage("user" if n % 2 else "assistant", "합성 문맥", "normal", n + 2)
                for n in range(distance)]
    assert crisis_guard.latest_intervention(history) == expected
    assert crisis_guard.latest_intervention([crisis_guard.GuardMessage("user", "합성", "safety")]) is None
    data, followup = crisis_guard.prepare_input("합성 현재 입력", history)
    assert len(data) == min(len(history), 6) + 1 and followup is (expected == "safety_check")
    assert data[-1] == {"role": "user", "content": "합성 현재 입력"}


@pytest.mark.parametrize("language,character", [("ko", "가"), ("en", "A"), ("ja", "あ"),
                                               ("ko", "😀"), ("en", "😀"), ("ja", "😀")])
def test_actual_foreground_persona_tools_and_large_required_input_fit(language, character):
    retention = chat.context_retention
    system = chat._build_system(language, "테스터", crisis_guard_enabled=True)
    # Facts use their 2048-unit bound; contract has no global maximum. This
    # explicitly tests 2048 bytes of preserved contract, not a fictional cap.
    required = "[Current verified facts]\n" + "F" * 2048 + "\n" + "C" * 2048
    messages = [{"role": "system", "content": required},
                {"role": "user", "content": character * 2000}]
    system, messages, tools = chat.agent_runtime.foreground_budget_inputs(
        system, messages, user_text=character * 2000,
    )
    assert tools  # Use the complete real registry, not an empty stand-in schema.
    assert retention.assert_input_budget(system, messages, tools=tools,
        input_limit=retention.FOREGROUND_INPUT_LIMIT) <= 32_768


def test_foreground_32k_does_not_raise_background_defaults():
    retention = chat.context_retention
    messages = [{"role": "user", "content": "x" * 20_000}]
    assert retention.fits_input_budget("system", messages, input_limit=retention.FOREGROUND_INPUT_LIMIT)
    assert not retention.fits_input_budget("system", messages)
    assert chat.checkpoint.BACKGROUND_INPUT_LIMIT == 16_384
    assert retention.MAX_CORRECTION_SOURCE_BYTES == 16_384


@pytest.mark.parametrize("raw_overflow", [False, True])
@pytest.mark.parametrize("uses_agent", [False, True])
async def test_large_immutable_contract_fails_without_a_history_recovery_job(setup, monkeypatch, raw_overflow, uses_agent):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))
    monkeypatch.setattr(chat.context_retention, "assert_input_budget", ACTUAL_ASSERT_BUDGET)
    monkeypatch.setattr(chat.context_retention, "fits_input_budget", ACTUAL_FITS_BUDGET)
    monkeypatch.setattr(chat.agent_runtime, "should_run", lambda *args: uses_agent)
    chat.contract_repo.published_text.return_value = "C" * 40_000
    if raw_overflow:
        monkeypatch.setattr(chat, "_context", AsyncMock(side_effect=chat._HistoryReadPressure))
    session = Session()
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(session, UID, request(), "irreducible-contract")
    assert caught.value.code == "AI_UNAVAILABLE"
    chat.checkpoint_repo.request_pressure.assert_not_awaited()
    chat.chat_turns.release.assert_awaited_once()
    assert session.added == []


@pytest.mark.parametrize("boundary", ["raw", "assembled"])
async def test_both_history_pressure_boundaries_use_one_fenced_committed_reservation(setup, monkeypatch, boundary):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))
    if boundary == "raw":
        monkeypatch.setattr(chat, "_context", AsyncMock(side_effect=chat._HistoryReadPressure))
    else:
        def too_large(*args, **kwargs):
            assert kwargs["input_limit"] == 32_768
            raise chat.context_retention.RetentionError("context_input_budget")
        monkeypatch.setattr(chat.context_retention, "assert_input_budget", too_large)
    verify = AsyncMock()
    monkeypatch.setattr(chat.chat_turns, "verify_publish", verify)
    session = Session()
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(session, UID, request(), "history-boundary")
    assert caught.value.code == "AI_UNAVAILABLE"
    chat.checkpoint_repo.request_pressure.assert_awaited_once()
    assert chat.checkpoint_repo.request_pressure.await_args.kwargs == {
        "user_id": chat._uid(UID), "generation": 0,
    }
    verify.assert_awaited_once()
    chat.chat_turns.release.assert_awaited_once()
    chat._accumulate_tokens.assert_not_awaited()
    chat.llm.generate.assert_not_awaited()
    assert session.added == [] and session.commits >= 2


@pytest.mark.parametrize("change", ["generation", "privacy", "recent_source"])
async def test_raw_pressure_preserves_common_privacy_and_generation_fences(setup, monkeypatch, change):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))
    monkeypatch.setattr(chat, "_context", AsyncMock(side_effect=chat._HistoryReadPressure))
    if change == "generation":
        monkeypatch.setattr(chat.chat_turns, "verify_publish", AsyncMock(side_effect=errors.AppError(
            "CHAT_TURN_STALE", 409, "synthetic stale generation")))
    elif change == "privacy":
        monkeypatch.setattr(chat.privacy, "ensure_subject_active", AsyncMock(side_effect=[
            None, errors.AppError("ACCOUNT_DELETED", 403, "synthetic deletion")]))
    else:
        chat._guard_recent.side_effect = [[], [crisis_guard.GuardMessage("user", "synthetic changed", message_id=9)]]
    session = Session()
    with pytest.raises(errors.AppError):
        await chat.post_message(session, UID, request(), "raw-source-fence")
    chat.checkpoint_repo.request_pressure.assert_not_awaited()
    assert session.added == []


@pytest.mark.parametrize("path", ["single", "tools", "length_recovery"])
async def test_agent_hop_and_recovery_keep_foreground_budget(path, monkeypatch):
    from tests.test_agent_runtime import (
        _FakeRegistry, _FakeSteps, _FakeTool, _call, _calls_step, _cfg, _factory,
        _text_step, _usage,
    )
    if path == "tools":
        first = _calls_step(_call(1))
    elif path == "length_recovery":
        first = llm.StepResult(text="synthetic incomplete", tool_calls=[],
                               finish_reason="length", usage=_usage("tool_decide"))
    else:
        first = _text_step("synthetic complete")
    steps = _FakeSteps(first, _text_step("synthetic complete", purpose="tool_final"))
    checked = []

    def budget(system, messages, **kwargs):
        assert kwargs["input_limit"] == 32_768
        checked.append(kwargs)
        return ACTUAL_ASSERT_BUDGET(system, messages, **kwargs)

    monkeypatch.setattr(chat.llm, "generate_step", steps)
    monkeypatch.setattr(chat.context_retention, "assert_input_budget", budget)
    await chat.agent_runtime.run_turn(
        ["x" * 20_000], [{"role": "user", "content": "synthetic current"}],
        config=_cfg(), user_id=chat._uid(UID), language="en", activity_date=date(2026, 11, 3),
        user_text="synthetic current", model="gpt-6-luna",
        registry=_FakeRegistry(_FakeTool()), session_factory=_factory, retention_enabled=True,
    )
    assert len(checked) == (1 if path == "single" else 2)


@pytest.mark.parametrize("recoverable", [True, False])
async def test_late_runtime_budget_overflow_becomes_one_pressure_pause_not_500(setup, monkeypatch, recoverable):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))
    monkeypatch.setattr(chat.agent_runtime, "should_run", lambda *args: True)
    monkeypatch.setattr(chat.agent_runtime, "run_turn", AsyncMock(side_effect=chat.context_retention.RetentionError(
        "context_input_budget")))
    monkeypatch.setattr(chat.context_retention, "fits_input_budget", lambda *args, **kwargs: recoverable)
    session = Session()
    with pytest.raises(errors.AppError) as caught:
        await chat.post_message(session, UID, request(), "late-runtime-overflow")
    assert caught.value.code == "AI_UNAVAILABLE"
    assert chat.checkpoint_repo.request_pressure.await_count == int(recoverable)
    chat.chat_turns.release.assert_awaited_once()
    chat._accumulate_tokens.assert_not_awaited()
    assert session.added == []


async def test_unrelated_runtime_retention_failure_is_not_relabelled_or_reserved(setup, monkeypatch):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", case="not_applicable")))
    monkeypatch.setattr(chat.agent_runtime, "should_run", lambda *args: True)
    monkeypatch.setattr(chat.agent_runtime, "run_turn", AsyncMock(side_effect=chat.context_retention.RetentionError(
        "synthetic_source_validation_failure")))
    session = Session()
    with pytest.raises(chat.context_retention.RetentionError, match="synthetic_source_validation_failure"):
        await chat.post_message(session, UID, request(), "unrelated-retention")
    chat.checkpoint_repo.request_pressure.assert_not_awaited()
    chat.chat_turns.release.assert_awaited_once()
    assert session.added == []
