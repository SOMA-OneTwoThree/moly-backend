"""Only an attested, exact covered prefix can leave an oversized runtime wire."""
from datetime import date

import pytest

from app.services import context_retention as cr, llm
from app.services.agent import runtime
from tests.test_agent_runtime import (_FakeRegistry, _FakeSteps, _FakeTool, _FakeToolSession,
                                      _call, _calls_step, _cfg, _text_step, _usage, UID)


PREFIX = ({"role": "user", "content": "Covered source"},
          {"role": "assistant", "content": "Covered response"})
TAIL = [{"role": "user", "content": "Uncovered source"},
        {"role": "assistant", "content": "Uncovered response"},
        {"role": "system", "content": "Required volatile facts"},
        {"role": "user", "content": "Current input"}]


def convo():
    return [*(dict(row) for row in PREFIX), *(dict(row) for row in TAIL)]


def body(messages):
    return " ".join(str(row.get("content", "")) for row in messages)


async def run(steps, *, registry=None, prefix=PREFIX, messages=None, retention=True):
    return await runtime.run_turn(["Required system"], messages or convo(), config=_cfg(),
        user_id=UID, language="en", activity_date=date(2026, 11, 3), user_text="Current input",
        registry=registry or _FakeRegistry(), session_factory=_FakeToolSession,
        retention_enabled=retention, retention_covered_prefix=prefix)


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


@pytest.mark.parametrize("prefix,retention", [
    (list(PREFIX), True),
    (({"role": "user", "content": "different uncovered source"},), True),
    (({"role": "system", "content": "Required system"},), True),
    (({"role": "user", "content": "Covered source", "extra": "value"},), True),
    (({"role": [], "content": "Covered source"},), True),
    (({"role": "user", "content": None},), True),
    (PREFIX, False),
])
async def test_invalid_or_mismatched_prefix_cannot_remove_any_input(monkeypatch, prefix, retention):
    steps = _FakeSteps(_text_step("done"))
    monkeypatch.setattr(llm, "generate_step", steps)
    with pytest.raises(ValueError, match="retention_covered_prefix_invalid"):
        await run(steps, prefix=prefix, retention=retention)
    assert not steps.calls


async def test_metadata_cannot_include_the_current_question(monkeypatch):
    steps = _FakeSteps(_text_step("done"))
    monkeypatch.setattr(llm, "generate_step", steps)
    with pytest.raises(ValueError, match="retention_covered_prefix_invalid"):
        await run(steps, messages=list(PREFIX), prefix=PREFIX)
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
