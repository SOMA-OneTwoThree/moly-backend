"""Generation-only safety projection; original guard and retention sources remain whole."""
from datetime import date
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import chat, context_retention, crisis_chat, crisis_guard
from app.services.context_provenance import VerifiedCoverage
from tests.test_chat import FakeSession, _Result, UID
from tests.test_guard_context_v2_integration_review import Session, request, setup as setup


def row(identity, sender, kind, content):
    return SimpleNamespace(id=identity, sender=sender, kind=kind, content=content,
        activity_date=date(2026, 11, 4), created_at=None)


def history():
    return [row(1, "moly", "safety", "prior fixed leading guide"),
        row(2, "user", "safety_check", "prior fixed check user " + "x" * 4400),
        row(3, "moly", "safety_check", "text shared with a normal message"),
        row(4, "moly", "normal", "normal leading response"),
        row(5, "user", "normal", "text shared with a normal message"),
        row(6, "moly", "normal", "normal response"),
        row(7, "user", "safety", "prior fixed guide user"),
        row(8, "moly", "safety", "prior fixed guide response"),
        row(9, "user", "fortune_context_root", "other kind remains"),
        row(10, "moly", "normal", "last normal response")]


class Rows(FakeSession):
    def __init__(self, rows, covered=()):
        super().__init__()
        self.results = [list(reversed(rows)), list(reversed(covered))]
        self.queries = []
    async def execute(self, statement, params=None):
        self.queries.append(statement)
        return _Result(self.results.pop(0) if self.results else [])


@pytest.mark.parametrize("retention", [True, False])
async def test_projection_filters_both_safety_kinds_and_senders_before_leading_extraction(retention):
    original = history()
    before = [(m.id, m.kind, m.content) for m in original]
    # A current question matching an old fixed guide must still be included.
    current = original[7].content
    options = dict(retention_enabled=retention, compact_crises=False, current_text=current,
                   current_date=date(2026, 11, 4), memory_generation=3)
    full_session, projected_session = Rows(original), Rows(original)
    full = await chat._context(full_session, chat._uid(UID), 0, **options)
    projected = await chat._context(projected_session, chat._uid(UID), 0,
        normal_generation_projection=True, **options)
    assert [m.id for m in projected.lead] == [4]
    assert all(m.kind not in crisis_guard.SAFETY_KINDS for m in projected.lead)
    assert [m["role"] for m in projected.conversation] == ["user", "assistant", "user", "assistant", "user"]
    for entry, source in zip(projected.conversation[:-1], [original[i] for i in (4, 5, 8, 9)], strict=True):
        assert entry["content"].endswith(source.content)
        if retention:
            assert entry.source_ref.message_id == source.id
            assert entry.source_ref.raw_content_hash == hashlib.sha256(source.content.encode()).hexdigest()
            assert entry.source_ref.memory_generation == 3
        else:
            assert entry.source_ref is None
    assert projected.conversation[-1]["content"].endswith(current)
    assert projected.conversation[-1].source_ref is None
    assert (projected.new_anchor, projected.checkpoint_keep_from, projected.incomplete,
            projected.retention_source_bytes) == (full.new_anchor, full.checkpoint_keep_from,
                                                 full.incomplete, full.retention_source_bytes)
    assert [str(q) for q in projected_session.queries] == [str(q) for q in full_session.queries]
    assert before == [(m.id, m.kind, m.content) for m in original]
    assert not full_session.added and not projected_session.added
    if retention:
        assert projected.checkpoint_keep_from is not None


async def test_projection_does_not_change_complete_coverage_bound_sources_or_raw_budget():
    original = history()
    coverage = VerifiedCoverage(chat._uid(UID), 3, uuid.UUID(int=9), 8, "a" * 64)
    full = await chat._context(Rows(original[8:], original[:8]), chat._uid(UID), 0,
        retention_enabled=True, memory_generation=3, coverage=coverage, current_text="current",
        compact_crises=False)
    projected = await chat._context(Rows(original[8:], original[:8]), chat._uid(UID), 0,
        retention_enabled=True, memory_generation=3, coverage=coverage, current_text="current",
        compact_crises=False, normal_generation_projection=True)
    assert full.coverage == projected.coverage
    assert {source.message_id for source in projected.coverage.source_refs} == set(range(2, 9))
    assert full.retention_source_bytes == projected.retention_source_bytes == context_retention.source_tokens(original[8:])
    assert full.checkpoint_keep_from == projected.checkpoint_keep_from
    assert full.new_anchor == projected.new_anchor


@pytest.mark.parametrize("retention", [True, False])
@pytest.mark.parametrize("with_current", [True, False])
async def test_all_safety_history_cannot_return_through_empty_history_fallback(retention, with_current):
    original = [row(1, "user", "safety_check", "old check user"),
                row(2, "moly", "safety", "old guide response")]
    result = await chat._context(Rows(original), chat._uid(UID), 0,
        retention_enabled=retention, compact_crises=False,
        normal_generation_projection=True, current_text="current" if with_current else None)
    assert not result.lead
    assert len(result.conversation) == int(with_current)
    if with_current:
        assert result.conversation[0]["content"].endswith("current")


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("status", ["ok", "error", "timeout", "refusal", "conflict", "invalid"])
async def test_public_normal_generation_gates_projection_on_success_only(setup, monkeypatch, enabled, status):
    _, configuration = setup
    configuration["crisis_guard_enabled"] = enabled
    decision = AsyncMock(return_value=crisis_guard.DecisionResult(status, route="normal", case="not_applicable"))
    monkeypatch.setattr(crisis_chat, "recorded_decision", decision)
    real_context = chat._context
    original = history()
    async def context(session, uid, anchor, **kwargs):
        return await real_context(Rows(original), uid, anchor, **kwargs)
    context_spy = AsyncMock(side_effect=context)
    monkeypatch.setattr(chat, "_context", context_spy)
    await chat.post_message(Session(), UID, request("current request"), "normal-input-view")
    projected = enabled and status == "ok"
    assert context_spy.await_args.kwargs["normal_generation_projection"] is projected
    assert decision.await_count == int(enabled)
    chat.llm.generate.assert_awaited_once()
    system, messages = chat.llm.generate.await_args.args[:2]
    wire = str(system) + str(messages)
    for text in ("prior fixed leading guide", "prior fixed check user", "prior fixed guide user"):
        assert (text not in wire) is projected
    assert all(text in wire for text in ("normal leading response", "text shared with a normal message",
                                         "normal response", "other kind remains", "current request"))


@pytest.mark.parametrize("route", ["ask", "safety"])
async def test_fixed_responses_keep_existing_early_return_without_context_or_generation(setup, monkeypatch, route):
    monkeypatch.setattr(crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route=route, case="self_thought")))
    context = AsyncMock(side_effect=AssertionError("fixed reply cannot prepare normal context"))
    monkeypatch.setattr(chat, "_context", context)
    await chat.post_message(Session(), UID, request(), "fixed-input-view")
    context.assert_not_awaited()
    chat.llm.generate.assert_not_awaited()


async def test_guard_recent_still_returns_six_original_messages_of_all_kinds():
    original = [history()[i] for i in (0, 1, 2, 4, 6, 8)]
    session = Rows(original)
    recent = await chat._guard_recent(session, chat._uid(UID), 6)
    assert [message.message_id for message in recent] == [m.id for m in original]
    assert [message.kind for message in recent] == [m.kind for m in original]
    assert [message.content for message in recent] == [m.content for m in original]
    query = session.queries[0]
    assert 6 in query.compile().params.values()
    assert "kind" not in str(query).split("WHERE", 1)[1]
