"""Raw identities survive all rendering; only exact verified sources are optional."""
from datetime import date
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import chat, checkpoint, checkpoint_repo, context_retention, llm, naming
from app.services.agent import runtime
from app.services.context_provenance import (
    HistoryEntry, SourceRef, VerifiedCoverage, ProvenanceError, without_covered, wire_messages,
)
from tests.test_chat import _Result
from tests.test_agent_runtime import _FakeSteps, _cfg, _text_step

UID = uuid.UUID(int=1)


def source(identity, text, generation=3):
    return SourceRef(UID, identity, generation, hashlib.sha256(text.encode()).hexdigest())


def grant(through=2, refs=()):
    return VerifiedCoverage(UID, 3, uuid.UUID(int=9), through, "a" * 64).bind_sources(refs)


def row(identity, role, text, kind="normal"):
    return SimpleNamespace(id=identity, sender=role, content=text, kind=kind,
                           activity_date=date(2026, 11, 2), created_at=None)


def test_rendering_and_copy_preserve_identity_but_wire_has_no_metadata():
    ref = source(1, "{유저이름} original")
    entry = HistoryEntry("user", "{유저이름} original", source_ref=ref)
    chat._mark_dates([entry], [SimpleNamespace(created_at=None)])
    entry["content"] = naming.render(entry["content"], "Synthetic")
    copied = entry.copy()
    typed = runtime._transcript([copied])[0]
    assert copied.source_ref == ref == typed.source_ref
    assert set(entry) == {"role", "content"}
    assert wire_messages([entry]) == [{"role": "user", "content": entry["content"]}]
    assert all(set(m) == {"role", "content"} for m in llm.to_openai_messages([], [typed]))


def test_identical_text_does_not_grant_current_volatile_tool_or_unknown_legacy_items():
    text = "identical content"
    covered = HistoryEntry("user", text, source_ref=source(1, text))
    uncovered = HistoryEntry("assistant", text, source_ref=source(8, text))
    current, volatile = HistoryEntry("user", text), HistoryEntry("system", text)
    legacy = {"role": "assistant", "content": text}
    typed_required = llm.SystemText(text)
    entries = [covered, uncovered, current, volatile, legacy, typed_required]
    assert without_covered(entries, grant(refs=[covered.source_ref])) == entries[1:]
    assert without_covered(entries, None) == entries
    changed_original = HistoryEntry("user", "changed", source_ref=source(1, "changed"))
    assert without_covered([changed_original], grant(refs=[covered.source_ref])) == [changed_original]


def test_nonconsecutive_global_ids_are_exact_members_not_an_integer_count():
    first, last = source(10, "first"), source(90, "last")
    coverage = grant(90, [first, last])
    assert coverage.covers(first) and coverage.covers(last)
    assert not coverage.covers(source(70, "not bound"))
    with pytest.raises(ProvenanceError, match="stale_source_identity"):
        coverage.covers(source(10, "first", generation=2))


@pytest.mark.parametrize("guard_enabled", [True, False])
async def test_sticky_retention_reader_never_runs_legacy_compaction_or_loses_uncovered(monkeypatch, guard_enabled):
    covered = [row(1, "user", "죽고 싶어. " + "가" * 900, "safety"),
               row(2, "moly", "응급 도움이 필요하면 119에 연락하세요.", "safety")]
    uncovered = [row(3, "user", "다른 얘기하자. 오늘 영화 봤어."), row(4, "moly", "영화는 어땠어?")]
    class Session:
        results = [list(reversed(uncovered)), list(reversed(covered))]
        async def execute(self, *args, **kwargs):
            return _Result(self.results.pop(0) if self.results else [])
    def forbidden(*args, **kwargs):
        pytest.fail("sticky retention reader must skip legacy compaction")
    monkeypatch.setattr(chat.context_safety, "compact_historical_crises", forbidden)
    window = await chat._context(Session(), UID, 3, checkpoint_through=2, coverage=grant(),
        memory_generation=3, current_text="주인공이 좋았어.", current_date=date(2026, 11, 2),
        language="ko", retention_enabled=True, compact_crises=not guard_enabled)
    assert [m.source_ref.message_id for m in window.conversation[:-1]] == [1, 2, 3, 4]
    remaining = without_covered(window.conversation, window.coverage)
    assert [m.source_ref.message_id for m in remaining[:-1]] == [3, 4]
    assert remaining[-1].source_ref is None


@pytest.mark.parametrize("version,processing,authorized", [
    (checkpoint.RETENTION_SUMMARIZER_VERSION, True, True),
    (checkpoint.RETENTION_SUMMARIZER_VERSION, False, False),
    (checkpoint.LEGACY_SUMMARIZER_VERSION, True, False),
])
async def test_coverage_uses_only_existing_current_generation_processing_gate(monkeypatch, version, processing, authorized):
    monkeypatch.setattr(checkpoint_repo, "load_compat_baseline", AsyncMock(return_value=None))
    value = checkpoint.Checkpoint(uuid.UUID(int=9), 2, "summary", version,
        "a" * 64, {"items": []}, {"processing_complete": True})
    check = AsyncMock(return_value=processing)
    monkeypatch.setattr(context_retention, "processing_complete", check)
    coverage = await checkpoint_repo.coverage_for_checkpoint(None, user_id=UID, generation=3, value=value)
    assert (coverage is not None) is authorized
    if version == checkpoint.RETENTION_SUMMARIZER_VERSION:
        assert check.await_args.kwargs == {"user_id": UID, "generation": 3, "through_id": 2}
    else:
        check.assert_not_awaited()
    if coverage is not None:
        assert coverage.memory_generation == 3 and not coverage.source_refs


async def test_actual_32k_runtime_fallback_preserves_all_uncovered_current_and_required(monkeypatch):
    covered = HistoryEntry("user", "c" * 3000, source_ref=source(1, "c" * 3000))
    uncovered = HistoryEntry("assistant", "uncovered", source_ref=source(3, "uncovered"))
    current = HistoryEntry("user", "current")
    required = HistoryEntry("system", "required facts")
    entries = [covered, uncovered, required, current]
    retained = [uncovered, required, current]
    system = ["required system"]
    def bound(sys, rows):
        return context_retention.assert_input_budget([], llm.to_openai_messages(
            runtime._runtime_system(sys), runtime._transcript(rows)), input_limit=10**7)
    system = [system[0] + "x" * (32768 - bound(system, retained))]
    assert bound(system, entries) > 32768 == bound(system, retained)
    steps = _FakeSteps(_text_step("completed"))
    monkeypatch.setattr(llm, "generate_step", steps)
    registry = SimpleNamespace(wire_schemas=lambda: [], input_models=lambda: {})
    await runtime.run_turn(system, entries, config=_cfg(), user_id=UID, language="en",
        activity_date=date(2026, 11, 2), user_text="current", registry=registry,
        retention_enabled=True, retention_coverage=grant(refs=[covered.source_ref]))
    assert [m.text for m in steps.calls[0]["transcript"]] == [m["content"] for m in retained]
