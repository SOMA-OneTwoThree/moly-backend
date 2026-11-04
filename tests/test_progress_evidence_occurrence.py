"""Repeated literal evidence selects an explicit verified source occurrence.

Pure validation and fake model calls only; no provider/DB access.
"""
import hashlib
import json
from dataclasses import replace

import pytest

from app.services import checkpoint, context_retention as retention, llm


OMITTED = object()


def change(quote, occurrence=OMITTED, *, source_id=1, item_id="topic"):
    evidence = {"message_id": source_id, "quote": quote}
    if occurrence is not OMITTED:
        evidence["occurrence"] = occurrence
    return {"op": "add", "item_id": item_id, "kind": "topic", "text": "Synthetic topic",
            "evidence": [evidence]}


def apply(message, update, *, parent=None):
    return retention.apply_delta(json.dumps({"base_checkpoint_id": parent,
        "source_message_ids": [message.id], "updates": [update]}, ensure_ascii=False),
        previous_state=None, previous_id=None, messages=[message])


def source(body):
    return checkpoint.SourceMessage(1, "user", "normal", body)


@pytest.mark.parametrize("body,quote", [
    ("🙂일 안 해. 일 해. 일 안 해. 일 안 해.", "일 안 해"),
    ("🙂not going; going; not going; not going", "not going"),
    ("🙂行かない。行く。行かない。行かない。", "行かない"),
    ("가🙂e\u0301|e\u0301|e\u0301", "e\u0301"),
])
@pytest.mark.parametrize("occurrence", [1, 2, 3])
def test_first_middle_last_literal_occurrence_maps_to_exact_utf8_span(body, quote, occurrence):
    result = apply(source(body), change(quote, occurrence))
    proof = result["items"][0]["evidence"][0]
    starts = [index for index in range(len(body)) if body.startswith(quote, index)]
    expected = len(body[:starts[occurrence - 1]].encode())
    assert proof == {"message_id": 1, "start_utf8": expected,
                     "end_utf8": expected + len(quote.encode()),
                     "source_hash": hashlib.sha256(body.encode()).hexdigest()}
    assert body.encode()[proof["start_utf8"]:proof["end_utf8"]].decode() == quote
    assert "occurrence" not in proof


@pytest.mark.parametrize("occurrence", [1, 2, 3])
def test_overlapping_matches_count_start_positions(occurrence):
    proof = apply(source("🙂aaaaa"), change("aaa", occurrence))["items"][0]["evidence"][0]
    assert proof["start_utf8"] == len("🙂".encode()) + occurrence - 1
    assert proof["end_utf8"] - proof["start_utf8"] == 3


@pytest.mark.parametrize("occurrence", [OMITTED, 1])
def test_unique_quote_retains_legacy_compatibility(occurrence):
    assert apply(source("one unique source"), change("unique", occurrence))["items"]


@pytest.mark.parametrize("body,quote,occurrence", [
    ("repeated repeated", "repeated", OMITTED),
    ("repeated repeated", "repeated", None),
    ("repeated repeated", "repeated", 3),
    ("unique", "unique", 2),
    ("aaaaa", "aaa", 4),
    ("actual source", "not present", 1),
])
def test_ambiguous_missing_or_out_of_range_occurrence_does_not_select_first(body, quote, occurrence):
    with pytest.raises(retention.RetentionError, match="evidence_quote"):
        apply(source(body), change(quote, occurrence))


@pytest.mark.parametrize("occurrence", [0, -1, True, False, 1.0, 1.5, "1", [], {}])
def test_occurrence_requires_positive_strict_integer(occurrence):
    with pytest.raises(retention.RetentionError, match="delta_schema"):
        apply(source("repeat repeat"), change("repeat", occurrence))


def test_fragment_local_count_adds_absolute_offset_and_whole_source_hash():
    prefix, fragment, suffix = "앞부분🙂repeat ", "repeat|repeat|repeat", " 마지막 repeat"
    whole = prefix + fragment + suffix
    message = replace(source(fragment), start_utf8=len(prefix.encode()),
        end_utf8=len((prefix + fragment).encode()), source_hash=hashlib.sha256(whole.encode()).hexdigest())
    proof = apply(message, change("repeat", 2))["items"][0]["evidence"][0]
    assert proof["start_utf8"] == len((prefix + "repeat|").encode())
    assert whole.encode()[proof["start_utf8"]:proof["end_utf8"]].decode() == "repeat"
    assert proof["source_hash"] == hashlib.sha256(whole.encode()).hexdigest()


def test_unique_inside_fragment_needs_no_ordinal_even_if_whole_source_repeats():
    whole, fragment, prefix = "same|🙂same|same", "🙂same", "same|"
    message = replace(source(fragment), start_utf8=len(prefix.encode()),
        end_utf8=len((prefix + fragment).encode()), source_hash=hashlib.sha256(whole.encode()).hexdigest())
    proof = apply(message, change("same"))["items"][0]["evidence"][0]
    assert proof["start_utf8"] == len((prefix + "🙂").encode())


@pytest.mark.parametrize("kind", ["safety", "safety_check", "fortune_context_root", "fortune_derived"])
def test_occurrence_cannot_authorize_policy_excluded_source(kind):
    with pytest.raises(retention.RetentionError, match="excluded_source"):
        apply(replace(source("repeat repeat"), kind=kind), change("repeat", 2))


def test_occurrence_cannot_authorize_a_different_source_or_parent():
    with pytest.raises(retention.RetentionError, match="evidence_source"):
        apply(source("repeat repeat"), change("repeat", 2, source_id=2))
    with pytest.raises(retention.RetentionError, match="delta_parent"):
        apply(source("repeat repeat"), change("repeat", 2), parent="different-parent")


def test_delta_prompt_requests_fragment_local_overlap_aware_ordinals():
    for language in ("ko", "en", "ja"):
        system = retention.build_delta_system(language)
        assert "모든 evidence에 occurrence를 명시" in system
        assert "겹치는 일치도" in system and "조각이나 호출이 바뀌면 다시 1부터" in system
        assert '"occurrence":1' in system


async def test_every_fragment_resets_ordinal_and_only_final_complete_chunk_advances(monkeypatch):
    original = source("앞🙂repeat|" * 400)
    pieces = checkpoint.source_segments(original)
    assert len(pieces) > 2
    calls, expected_starts = [], []

    async def generate(system, messages, **kwargs):
        data = json.loads(messages[0]["content"])
        fragment = data["messages"][0]["content"]
        piece = pieces[len(calls)]
        assert fragment == piece.content
        first = fragment.find("repeat")
        second = fragment.find("repeat", first + 1)
        assert first >= 0 and second >= 0
        expected_starts.append(piece.start_utf8 + len(fragment[:second].encode()))
        calls.append(data)
        raw = {"base_checkpoint_id": None, "source_message_ids": [1],
               "updates": [change("repeat", 2, item_id=f"fragment-{len(calls)}")]}
        return llm.LLMResult(json.dumps(raw), 3, 3, finish_reason="stop")

    monkeypatch.setattr(llm, "generate", generate)
    continuation, start, manifest = None, 0, None
    while True:
        try:
            _, state, _ = await checkpoint.summarize_progress(messages=[original], previous=None,
                language="ko", nickname=None, continuation_state=continuation,
                start_chunk=start, continuation_manifest=manifest, max_batches=1)
            break
        except checkpoint.ProgressContinuation as pending:
            assert pending.state["sequence"] == 0
            continuation, start, manifest = pending.state, pending.next_chunk, pending.manifest
    assert len(calls) == len(pieces) and state["sequence"] == 1
    proofs = [item["evidence"][0] for item in state["items"]]
    assert {proof["start_utf8"] for proof in proofs} == set(expected_starts)
    for proof in proofs:
        assert set(proof) == {"message_id", "start_utf8", "end_utf8", "source_hash"}
        assert proof["source_hash"] == hashlib.sha256(original.content.encode()).hexdigest()
        assert original.content.encode()[proof["start_utf8"]:proof["end_utf8"]] == b"repeat"
