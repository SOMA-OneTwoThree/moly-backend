"""Only original IDs in verified coverage may leave an oversized runtime wire."""
from datetime import date
import hashlib
import uuid

import pytest

from app.services import context_retention as cr, llm
from app.services.agent import runtime
from app.services.context_provenance import HistoryEntry, SourceRef, VerifiedCoverage, ProvenanceError
from tests.test_agent_runtime import (_FakeRegistry, _FakeSteps, _FakeTool, _FakeToolSession,
                                      _call, _calls_step, _cfg, _text_step, _usage, UID)


def source_entry(identity, role, text, *, user=UID, generation=0):
    return HistoryEntry(role, text, source_ref=SourceRef(user, identity, generation,
                        hashlib.sha256(text.encode()).hexdigest()))


PREFIX = (source_entry(1, "user", "Covered source"),
          source_entry(2, "assistant", "Covered response"))
COVERAGE = VerifiedCoverage(UID, 0, uuid.UUID(int=2), 2, "a" * 64).bind_sources(
    row.source_ref for row in PREFIX)
TAIL = [source_entry(3, "user", "Uncovered source"),
        source_entry(4, "assistant", "Uncovered response"),
        {"role": "system", "content": "Required volatile facts"},
        {"role": "user", "content": "Current input"}]


def convo():
    return [*(row.copy() for row in PREFIX), *(row.copy() for row in TAIL)]


def body(messages):
    return " ".join(str(row.get("content", "")) for row in messages)


async def run(steps, *, registry=None, coverage=COVERAGE, messages=None, retention=True):
    return await runtime.run_turn(["Required system"], messages or convo(), config=_cfg(),
        user_id=UID, language="en", activity_date=date(2026, 11, 3), user_text="Current input",
        registry=registry or _FakeRegistry(), session_factory=_FakeToolSession,
        retention_enabled=retention, retention_coverage=coverage)


async def test_first_hop_omits_only_exact_covered_prefix_without_an_extra_model_call(monkeypatch):
    boundaries = []
    def budget(system, messages, **kwargs):
        boundaries.append(messages)
        if "Covered source" in body(messages):
            raise cr.RetentionError("context_input_budget")
    monkeypatch.setattr(cr, "assert_input_budget", budget)
    steps = _FakeSteps(_text_step("done"))
    monkeypatch.setattr(llm, "generate_step", steps)
    assert (await run(steps)).text == "done"
    assert len(steps.calls) == 1 and len(boundaries) == 2
    sent = llm.to_openai_messages(["Required system"], steps.calls[0]["transcript"])
    assert "Covered source" not in body(sent)
    assert all(row["content"] in body(sent) for row in TAIL)


async def test_final_hop_can_omit_prefix_and_preserves_all_tool_history(monkeypatch):
    boundaries = []
    def budget(system, messages, **kwargs):
        boundaries.append(messages)
        if any(row["role"] == "tool" for row in messages) and "Covered source" in body(messages):
            raise cr.RetentionError("context_input_budget")
    monkeypatch.setattr(cr, "assert_input_budget", budget)
    tool = _FakeTool(data={"items": ["Required tool result"]})
    steps = _FakeSteps(_calls_step(_call(1)), _text_step("done", purpose="tool_final"))
    monkeypatch.setattr(llm, "generate_step", steps)
    await run(steps, registry=_FakeRegistry(tool))
    assert len(steps.calls) == 2 and tool.ran == 1 and len(boundaries) == 3
    sent = llm.to_openai_messages(["Required system"], steps.calls[-1]["transcript"])
    assert "Covered source" not in body(sent)
    assert all(row["content"] in body(sent) for row in TAIL)
    assert "Required tool result" in body(sent)
    assert any(row.get("tool_calls") for row in sent)


async def test_length_recovery_keeps_its_instruction_and_drops_only_covered_prefix(monkeypatch):
    def budget(system, messages, **kwargs):
        if "[Length recovery]" in body(messages) and "Covered source" in body(messages):
            raise cr.RetentionError("context_input_budget")
    monkeypatch.setattr(cr, "assert_input_budget", budget)
    first = llm.StepResult(text="unfinished", tool_calls=[], finish_reason="length", usage=_usage("tool_decide"))
    steps = _FakeSteps(first, _text_step("done"))
    monkeypatch.setattr(llm, "generate_step", steps)
    await run(steps, registry=_FakeRegistry(_FakeTool()))
    assert len(steps.calls) == 2
    sent = llm.to_openai_messages([], steps.calls[-1]["transcript"])
    assert "Covered source" not in body(sent) and "[Length recovery]" in body(sent)
    assert all(row["content"] in body(sent) for row in TAIL)


async def test_second_overflow_never_drops_uncovered_rows_a_second_time(monkeypatch):
    seen = []
    def budget(system, messages, **kwargs):
        seen.append(messages)
        if "Covered source" in body(messages) or any(row["role"] == "tool" for row in messages):
            raise cr.RetentionError("context_input_budget")
    monkeypatch.setattr(cr, "assert_input_budget", budget)
    steps = _FakeSteps(_calls_step(_call(1)))
    monkeypatch.setattr(llm, "generate_step", steps)
    with pytest.raises(cr.RetentionError, match="context_input_budget"):
        await run(steps, registry=_FakeRegistry(_FakeTool()))
    assert len(steps.calls) == 1 and len(seen) == 3
    assert all(row["content"] in body(seen[-1]) for row in TAIL)


@pytest.mark.parametrize("user,generation", [(uuid.UUID(int=99), 0), (UID, 1)])
async def test_stale_explicit_source_is_rejected_even_when_the_wire_fits(monkeypatch, user, generation):
    messages = [source_entry(1, "user", "source", user=user, generation=generation),
                {"role": "user", "content": "Current input"}]
    steps = _FakeSteps(_text_step("done"))
    monkeypatch.setattr(llm, "generate_step", steps)
    with pytest.raises(ProvenanceError, match="stale_source_identity"):
        await run(steps, messages=messages)
    assert not steps.calls


async def test_metadata_cannot_include_the_current_question(monkeypatch):
    steps = _FakeSteps(_text_step("done"))
    monkeypatch.setattr(llm, "generate_step", steps)
    with pytest.raises(ProvenanceError, match="current_input_has_source"):
        await run(steps, messages=list(PREFIX))
    assert not steps.calls


async def test_legacy_plain_items_never_get_permission_from_matching_text(monkeypatch):
    messages = [dict(row) for row in convo()]
    steps = _FakeSteps(_text_step("done"))
    monkeypatch.setattr(llm, "generate_step", steps)
    def budget(*args, **kwargs):
        raise cr.RetentionError("context_input_budget")
    monkeypatch.setattr(cr, "assert_input_budget", budget)
    with pytest.raises(cr.RetentionError, match="context_input_budget"):
        await run(steps, messages=messages)
    assert not steps.calls


async def test_nonbudget_errors_do_not_remove_optional_prefix(monkeypatch):
    def budget(*args, **kwargs):
        raise cr.RetentionError("unrelated_contract_error")
    monkeypatch.setattr(cr, "assert_input_budget", budget)
    steps = _FakeSteps(_text_step("done"))
    monkeypatch.setattr(llm, "generate_step", steps)
    with pytest.raises(cr.RetentionError, match="unrelated_contract_error"):
        await run(steps)
    assert not steps.calls
