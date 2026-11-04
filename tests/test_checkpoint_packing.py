"""Versioned packing preserves the old atomic source plan and proof boundaries."""
import copy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import checkpoint as cp, context_retention as cr, llm


def source(text="x" * 24000, identity=1):
    return cp.SourceMessage(identity, "user", "normal", text)


def parent(sequence=4):
    return cp.Checkpoint(uuid.UUID(int=9), 9, "", cp.RETENTION_SUMMARIZER_VERSION,
                         "a" * 64, {**cr.empty_state(), "sequence": sequence})


def item(identity, original, *, status="active", size=600):
    return {"id": identity, "kind": "topic", "text": "a" * size, "status": status,
        "changed_sequence": 1, "idle_checkpoints": 0, "evidence": [{"message_id": original.id,
            "start_utf8": 0, "end_utf8": 1,
            "source_hash": hashlib.sha256(original.content.encode()).hexdigest()}]}


@pytest.fixture
def model(monkeypatch):
    calls = []
    async def generate(system, messages, **kwargs):
        cr.assert_input_budget(system, messages, input_limit=16384, output_reserve=2000)
        body = json.loads(messages[0]["content"])
        calls.append(body)
        return llm.LLMResult(json.dumps({"base_checkpoint_id": body["base_checkpoint_id"],
            "source_message_ids": body["source_message_ids"], "updates": []}), 10, 2,
            finish_reason="stop")
    monkeypatch.setattr(llm, "generate", generate)
    return calls


async def run(messages, **kwargs):
    return await cp.summarize_progress(messages=messages, previous=kwargs.pop("previous", None),
        language="ko", nickname=None, packing_version=cp.PACKING_VERSION, **kwargs)


def page_options(pending, messages, *, previous=None, cap=None):
    return dict(messages=messages, previous=previous, language="ko", state=pending.state,
        previous_state=pending.before_state, start_chunk=pending.page_start,
        next_chunk=pending.next_chunk, manifest=pending.manifest, page_proof=pending.page_proof,
        packing_version=cp.PACKING_VERSION, packing_chunk_cap=cap)


def completed_options(result, messages, *, previous=None, cap=None):
    return dict(messages=messages, previous=previous, language="ko", state=result.state,
        previous_state=result.before_state, start_chunk=result.page_start, page_proof=result.page_proof,
        packing_version=cp.PACKING_VERSION, packing_chunk_cap=cap)


async def test_default_legacy_stays_atomic_but_packed_combines_full_sources(model):
    messages = [source("가" * 680, identity=i) for i in (3, 1, 2)]
    atoms = cp.progress_batches(messages, previous=None, language="ko")
    assert len(atoms) == 3
    with pytest.raises(cp.ProgressContinuation) as old:
        await cp.summarize_progress(messages=messages, previous=None, language="ko", nickname=None)
    assert old.value.next_chunk == 1 and old.value.page_proof is None
    model.clear()
    result = await run(messages)
    summary, state, call = result
    assert len(model) == 1 and model[0]["source_message_ids"] == [1, 2, 3]
    assert state["sequence"] == 1 and call.billable > 0 and summary == ""
    assert result.page_proof["next_chunk"] == 3
    assert cp.progress_batches(messages, previous=None, language="ko") == atoms


@pytest.mark.parametrize("pattern", ["한글🙂", 'quote"\\\n🙂', "繰り返し🙂", "e\u0301🙂"])
async def test_packed_pages_reconstruct_every_utf8_byte_and_advance_once(model, pattern):
    original = source(pattern * 2000)
    before, start, manifest, proof = None, 0, None, None
    while True:
        try:
            result = await run([original], continuation_state=before, start_chunk=start,
                continuation_manifest=manifest, continuation_proof=proof, max_batches=1)
            break
        except cp.ProgressContinuation as pending:
            assert pending.state["sequence"] == 0
            cp.validate_progress_page(**page_options(pending, [original]))
            before, start, manifest, proof = pending.state, pending.next_chunk, pending.manifest, pending.page_proof
    assert "".join(call["messages"][0]["content"] for call in model) == original.content
    assert all(call["source_message_ids"] == [1] for call in model)
    assert result.state["sequence"] == 1
    proof = cp.completed_progress_proof(**completed_options(result, [original]))
    assert proof["completed_chunks"] == len(cp.progress_batches([original], previous=None, language="ko"))
    assert cp.validate_completed_progress(**completed_options(result, [original]), proof=proof) == ""


def test_growing_actual_state_reduces_the_largest_contiguous_pack():
    original = source()
    atoms = cp.progress_batches([original], previous=None, language="ko")
    first = cp.packed_progress_batch(atoms, previous=None, language="ko", state=cr.empty_state(), start_chunk=0)
    grown = {**cr.empty_state(), "items": [item(f"i{i}", original) for i in range(3)]}
    empty_next = cp.packed_progress_batch(atoms, previous=None, language="ko", state=cr.empty_state(),
                                        start_chunk=first.next_chunk)
    grown_next = cp.packed_progress_batch(atoms, previous=None, language="ko", state=grown,
                                        start_chunk=first.next_chunk)
    assert grown_next.next_chunk < empty_next.next_chunk
    cr.assert_input_budget(cr.build_delta_system("ko"), [{"role": "user", "content": cr.build_delta_input(
        previous_state=grown, previous_id=None, messages=grown_next.messages, compact_previous=False)}],
        input_limit=16384, output_reserve=2000)
    with pytest.raises(cr.RetentionError, match="context_input_budget"):
        cr.assert_input_budget(cr.build_delta_system("ko"), [{"role": "user", "content": cr.build_delta_input(
            previous_state=grown, previous_id=None,
            messages=cp._coalesce_atoms(atoms[grown_next.start_chunk:grown_next.next_chunk+1]),
            compact_previous=False)}], input_limit=16384, output_reserve=2000)


def test_real_working_state_that_cannot_fit_one_atom_does_not_drop_state_or_source():
    original = source()
    atoms = cp.progress_batches([original], previous=None, language="ko")
    working = {**cr.empty_state(), "items": [item(f"i{i}", original, status="dormant") for i in range(20)]}
    original_state, original_atoms = copy.deepcopy(working), copy.deepcopy(atoms)
    cr.validate_state(working)
    with pytest.raises(cp.CheckpointError, match="progress_background_budget"):
        cp.packed_progress_batch(atoms, previous=None, language="ko", state=working, start_chunk=1)
    assert working == original_state and atoms == original_atoms


@pytest.mark.parametrize("change", [
    {"sender": "moly"}, {"kind": "greeting"}, {"source_hash": "b" * 64},
    {"created_at": datetime(2026, 11, 4, tzinfo=timezone.utc)},
    {"start_utf8": 2049, "end_utf8": 4001}, {"start_utf8": 0, "end_utf8": 1952},
])
def test_same_id_fragments_require_identical_metadata_and_exact_contiguity(change):
    pieces = cp.source_segments(source("x" * 4000))
    with pytest.raises(cp.CheckpointError):
        cp._coalesce_atoms([(pieces[0],), (replace(pieces[1], **change),)])


async def test_coalesced_repeated_evidence_keeps_absolute_offsets_and_whole_hash(monkeypatch):
    original, fragments = source("앞🙂repeat|" * 2000), []
    expected = []
    async def generate(system, messages, **kwargs):
        body = json.loads(messages[0]["content"])
        text = body["messages"][0]["content"]
        position = text.find("repeat", text.find("repeat") + 1)
        expected.append(sum(len(f.encode()) for f in fragments) + len(text[:position].encode()))
        fragments.append(text)
        return llm.LLMResult(json.dumps({"base_checkpoint_id": None, "source_message_ids": [1],
            "updates": [{"op": "add", "item_id": f"i{len(fragments)}", "kind": "topic", "text": "topic",
                "evidence": [{"message_id": 1, "quote": "repeat", "occurrence": 2}]}]}), 1, 2,
            finish_reason="stop")
    monkeypatch.setattr(llm, "generate", generate)
    state, start, manifest, proof = None, 0, None, None
    while True:
        try:
            result = await run([original], continuation_state=state, start_chunk=start,
                continuation_manifest=manifest, continuation_proof=proof)
            break
        except cp.ProgressContinuation as pending:
            state, start, manifest, proof = pending.state, pending.next_chunk, pending.manifest, pending.page_proof
    assert "".join(fragments) == original.content
    refs = [entry["evidence"][0] for entry in result.state["items"]]
    assert sorted(ref["start_utf8"] for ref in refs) == sorted(expected)
    for ref in refs:
        assert ref["source_hash"] == hashlib.sha256(original.content.encode()).hexdigest()
        assert original.content.encode()[ref["start_utf8"]:ref["end_utf8"]].decode() == "repeat"


async def test_page_proof_replans_before_state_and_rejects_forged_end_even_with_new_checksum(model):
    messages = [source("x" * 2000, i) for i in (1, 2, 3)]
    with pytest.raises(cp.ProgressContinuation) as caught:
        await run(messages, packing_chunk_cap=1)
    pending = caught.value
    options = page_options(pending, messages, cap=1)
    cp.validate_progress_page(**options)
    atoms = cp.progress_batches(messages, previous=None, language="ko")
    forged = {**pending.page_proof, "next_chunk": 2,
              "source_span_hash": cp._packed_sources_hash(cp._coalesce_atoms(atoms[:2]))}
    forged["proof_hash"] = cp._digest({k: v for k, v in forged.items() if k != "proof_hash"})
    with pytest.raises(cp.CheckpointError, match="proof"):
        cp.validate_progress_page(**{**options, "next_chunk": 2, "page_proof": forged})
    for field, replacement in (("packing_version", "unknown"), ("manifest", "changed"),
                               ("before_state_hash", "b" * 64), ("after_state_hash", "c" * 64)):
        forged = {**pending.page_proof, field: replacement}
        with pytest.raises(cp.CheckpointError):
            cp.validate_progress_page(**{**options, "page_proof": forged})


async def test_resume_rejects_changed_state_and_cap_reexpansion_before_call(model):
    messages = [source("x" * 2000, i) for i in (1, 2, 3)]
    with pytest.raises(cp.ProgressContinuation) as caught:
        await run(messages, packing_chunk_cap=1)
    pending = caught.value
    before_call = AsyncMock()
    bad = {**pending.state, "items": [item("injected", messages[0], size=3)]}
    for state, cap in ((bad, 1), (pending.state, None)):
        with pytest.raises(cp.CheckpointError):
            await run(messages, continuation_state=state, start_chunk=pending.next_chunk,
                continuation_manifest=pending.manifest, continuation_proof=pending.page_proof,
                packing_chunk_cap=cap, before_call=before_call)
    before_call.assert_not_awaited()
    assert len(model) == 1


async def test_resume_replans_previous_page_instead_of_trusting_a_recomputed_checksum(model):
    messages = [source("x" * 2000, i) for i in (1, 2, 3)]
    with pytest.raises(cp.ProgressContinuation) as caught:
        await run(messages, packing_chunk_cap=1)
    pending = caught.value
    atoms = cp.progress_batches(messages, previous=None, language="ko")
    forged = {**pending.page_proof, "next_chunk": 2,
        "source_span_hash": cp._packed_sources_hash(cp._coalesce_atoms(atoms[:2]))}
    forged["proof_hash"] = cp._digest({k: v for k, v in forged.items() if k != "proof_hash"})
    before_call = AsyncMock()
    with pytest.raises(cp.CheckpointError, match="proof"):
        await run(messages, continuation_state=pending.state, start_chunk=2,
            continuation_manifest=pending.manifest, continuation_proof=forged,
            packing_chunk_cap=1, before_call=before_call)
    assert len(model) == 1
    before_call.assert_not_awaited()


async def test_multiple_packed_calls_replan_growth_and_invoke_callback_only_for_real_calls(monkeypatch):
    original, calls, charged = source("x" * 30000), [], []
    async def before_call():
        charged.append(True)
    async def generate(system, messages, **kwargs):
        cr.assert_input_budget(system, messages, input_limit=16384, output_reserve=2000)
        assert len(charged) == len(calls) + 1
        body = json.loads(messages[0]["content"])
        updates = [{"op": "add", "item_id": f"i{i}", "kind": "topic", "text": "a" * 600,
            "evidence": [{"message_id": 1, "quote": "x", "occurrence": 1}]} for i in range(3)] if not calls else []
        calls.append(body)
        return llm.LLMResult(json.dumps({"base_checkpoint_id": None, "source_message_ids": [1],
            "updates": updates}), 1, 2, finish_reason="stop")
    monkeypatch.setattr(llm, "generate", generate)
    result = await run([original], max_batches=100, before_call=before_call)
    assert len(calls) == len(charged) > 2
    assert len(calls[1]["messages"][0]["content"]) < len(calls[0]["messages"][0]["content"])
    assert "".join(c["messages"][0]["content"] for c in calls) == original.content
    assert result.state["sequence"] == 1 and len(result.state["items"]) == 3
    proof = cp.completed_progress_proof(**completed_options(result, [original]))
    assert proof["completed_chunks"] == len(cp.progress_batches([original], previous=None, language="ko"))


async def test_completed_proof_binds_version_all_atoms_and_final_state(model):
    messages = [source("x" * 2000, i) for i in (10, 11)]
    result = await run(messages, previous=parent())
    options = completed_options(result, messages, previous=parent())
    proof = cp.completed_progress_proof(**options)
    assert result.state["sequence"] == 5
    assert cp.validate_completed_progress(**options, proof=proof) == ""
    for field, replacement in (("completed_chunks", 1), ("packing_version", "unknown"), ("state_hash", "bad")):
        with pytest.raises(cp.CheckpointError):
            cp.validate_completed_progress(**options, proof={**proof, field: replacement})
    old_state = {**cr.empty_state(), "sequence": 5}
    old = cp.completed_progress_proof(messages=messages, previous=parent(), language="ko", state=old_state)
    assert "packing_version" not in old
    assert cp.validate_completed_progress(messages=messages, previous=parent(), language="ko",
        state=old_state, proof=old) == ""


def test_empty_policy_only_completion_uses_no_page_and_preserves_parent_state():
    state = copy.deepcopy(parent().state_json)
    proof = cp.completed_progress_proof(messages=[], previous=parent(), language="ko", state=state,
                                         packing_version=cp.PACKING_VERSION)
    assert proof["completed_chunks"] == 0 and proof["page_proof"] is None
    assert cp.validate_completed_progress(messages=[], previous=parent(), language="ko", state=state,
        proof=proof, packing_version=cp.PACKING_VERSION) == ""


@pytest.mark.parametrize("version", ["unknown", "", True, 1])
async def test_unknown_strategy_never_calls_model_or_before_call(monkeypatch, version):
    model, before = AsyncMock(), AsyncMock()
    monkeypatch.setattr(llm, "generate", model)
    with pytest.raises(cp.CheckpointError, match="packing_version"):
        await cp.summarize_progress(messages=[source("short")], previous=None, language="ko", nickname=None,
            packing_version=version, before_call=before)
    before.assert_not_awaited()
    model.assert_not_awaited()


async def test_enlarged_length_is_typed_and_applies_no_delta_but_single_atom_keeps_old_error(monkeypatch):
    original = source("x" * 4000)
    model = AsyncMock(return_value=llm.LLMResult("incomplete", 1, 2000, finish_reason="length"))
    monkeypatch.setattr(llm, "generate", model)
    apply = AsyncMock(side_effect=AssertionError("truncated output cannot be applied"))
    monkeypatch.setattr(cr, "apply_delta", apply)
    before = AsyncMock()
    with pytest.raises(cp.ExpandedProgressLength) as caught:
        await run([original], before_call=before)
    assert caught.value.start_chunk == 0 and caught.value.consumed_atoms == 2
    assert caught.value.before_state == cr.empty_state()
    assert before.await_count == 1
    with pytest.raises(llm.IncompleteResponseError):
        await run([original], packing_chunk_cap=1)
    apply.assert_not_awaited()


async def test_one_atom_over_budget_never_invokes_precharge_or_model(monkeypatch):
    model, before = AsyncMock(), AsyncMock()
    monkeypatch.setattr(llm, "generate", model)
    monkeypatch.setattr(cr, "build_delta_system", lambda language: "x" * 20000)
    with pytest.raises(cp.CheckpointError, match="budget"):
        await run([source("short")], before_call=before)
    before.assert_not_awaited()
    model.assert_not_awaited()


async def test_packed_system_change_stops_resume_before_another_model_call(model, monkeypatch):
    messages = [source()]
    with pytest.raises(cp.ProgressContinuation) as caught:
        await run(messages)
    pending = caught.value
    calls_before = len(model)
    original = cp._packed_delta_system
    monkeypatch.setattr(cp, "_packed_delta_system", lambda language: original(language) + " changed")
    with pytest.raises(cp.CheckpointError, match="manifest"):
        await run(messages, continuation_state=pending.state, start_chunk=pending.next_chunk,
                  continuation_manifest=pending.manifest, continuation_proof=pending.page_proof)
    assert len(model) == calls_before


def test_packed_system_change_does_not_reinterpret_legacy_manifest(monkeypatch):
    atoms = cp.progress_batches([source()], previous=None, language="ko")
    legacy = cp._progress_manifest(atoms, previous=None, language="ko")
    packed = cp._packed_manifest(atoms, previous=None, language="ko")
    monkeypatch.setattr(cp, "_packed_delta_system", lambda language: "different packed instructions")
    assert cp._progress_manifest(atoms, previous=None, language="ko") == legacy
    assert cp._packed_manifest(atoms, previous=None, language="ko") != packed
