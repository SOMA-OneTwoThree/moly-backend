"""구조화 상태의 보존·근거·원문 처리 경계 회귀. 외부 모델/DB 호출 없음."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.services import checkpoint, context_retention as retention, contract_compiler as cc
from app.services import interaction_contract as ic, mem0_extractor
from worker.contract_jobs import merge_retained_items


def source(message_id=1, text="다음 주 발표를 준비하고 있어", kind="normal", sender="user"):
    return checkpoint.SourceMessage(message_id, sender, kind, text)


def delta(*updates, parent=None, ids=(1,)):
    return json.dumps({"base_checkpoint_id": str(parent) if parent else None,
                       "source_message_ids": list(ids), "updates": list(updates)}, ensure_ascii=False)


def update(op="add", item_id="presentation", kind="open_question", text="발표 준비",
           quote="발표를 준비", message_id=1):
    return {"op": op, "item_id": item_id, "kind": kind, "text": text,
            "evidence": [{"message_id": message_id, "quote": quote}]}


def initial(kind="open_question"):
    return retention.apply_delta(delta(update(kind=kind)), previous_state=None,
                                 previous_id=None, messages=[source()])


def test_omitted_open_question_survives_ten_thousand_checkpoints():
    state = initial()
    expected = copy.deepcopy(state["items"][0])
    for _ in range(10_000):
        state = retention.apply_delta(delta(), previous_state=state, previous_id=None, messages=[source()])
    item = state["items"][0]
    assert item["status"] == "active"
    assert item["text"] == expected["text"] and item["evidence"] == expected["evidence"]


def test_topic_becomes_dormant_without_deleting_its_evidence():
    state = initial("topic")
    for _ in range(3):
        state = retention.apply_delta(delta(), previous_state=state, previous_id=None, messages=[source()])
    assert state["items"][0]["status"] == "dormant"
    assert state["items"][0]["evidence"]
    assert retention.render_state(state) == ""


def test_resolve_requires_new_evidence_and_preserves_old_body():
    state = initial()
    result = retention.apply_delta(delta(update(op="resolve", text="완전히 다른 내용",
        quote="발표 끝났어"), ids=(1,)), previous_state=state, previous_id=None,
        messages=[source(text="발표 끝났어")])
    assert result["items"][0]["status"] == "resolved"
    assert result["items"][0]["text"] == state["items"][0]["text"]
    assert len(result["items"][0]["evidence"]) == 2


def test_one_thousand_topics_archive_only_after_published_inactive_body():
    state, parent, history = None, None, []
    for index in range(1_000):
        message = source(index + 1, f"topic {index}: " + "x"*160)
        state = retention.apply_delta(delta(update(item_id=f"topic-{index}", kind="topic",
            text=message.content, quote=message.content, message_id=message.id),
            parent=parent, ids=(message.id,)), previous_state=state,
            previous_id=parent, messages=[message])
        history.append(copy.deepcopy(state))  # 성공 게시를 흉내 낸 불변 이력
        parent = uuid.uuid4()
        assert len(state["items"]) <= 4
    assert len(history) == 1_000
    first = [item for checkpoint_state in history for item in checkpoint_state["items"]
             if item["id"] == "topic-0"]
    assert first[-1]["status"] == "dormant" and first[-1]["text"].endswith("x"*160)
    assert not any(item["id"] == "topic-0" for item in state["items"])


def test_split_source_records_whole_source_span_and_ages_only_once():
    original = "가나다 발표 준비해"
    message = SimpleNamespace(id=1, sender="user", kind="normal", content="발표 준비해",
        start_utf8=len("가나다 ".encode()), end_utf8=len(original.encode()),
        source_hash=hashlib.sha256(original.encode()).hexdigest())
    parent = uuid.uuid4()
    state = initial("topic")
    state = retention.apply_delta(delta(update(op="amend", kind="topic", quote="발표 준비"),
        parent=parent), previous_state=state, previous_id=parent, messages=[message],
        advance_checkpoint=False)
    assert state["sequence"] == 1
    state = retention.apply_delta(delta(parent=parent), previous_state=state,
        previous_id=parent, messages=[source()], compact_previous=False)
    item = state["items"][0]
    assert state["sequence"] == 2 and item["idle_checkpoints"] == 0
    evidence = item["evidence"][0]
    assert original.encode()[evidence["start_utf8"]:evidence["end_utf8"]].decode() == "발표 준비"
    assert evidence["source_hash"] == hashlib.sha256(original.encode()).hexdigest()


def test_model_state_projection_omits_archived_bodies_and_old_evidence():
    state = initial("topic")
    state["items"][0]["status"] = "dormant"
    value = json.loads(retention.build_delta_input(previous_state=state,
        previous_id=uuid.uuid4(), messages=[source()]))
    assert value["previous_state"]["items"] == []
    state["items"][0]["status"] = "active"
    value = json.loads(retention.build_delta_input(previous_state=state,
        previous_id=uuid.uuid4(), messages=[source()]))
    assert set(value["previous_state"]["items"][0]) == {"id", "kind", "text", "status"}


def test_resolved_question_archives_after_one_published_termination():
    state, parent = initial(), uuid.uuid4()
    state = retention.apply_delta(delta(update(op="resolve", quote="발표 끝났어"), parent=parent),
        previous_state=state, previous_id=parent, messages=[source(text="발표 끝났어")])
    assert state["items"][0]["status"] == "resolved"
    stored = copy.deepcopy(state)
    state = retention.apply_delta(delta(parent=parent), previous_state=state,
        previous_id=parent, messages=[source()])
    assert not state["items"] and stored["items"][0]["evidence"]
    # 이력에서 찾은 같은 ID도 현재 원문 근거를 제공해야 새 작업 목록으로 돌아온다.
    state = retention.apply_delta(delta(update(quote="다시 발표 준비"), parent=parent),
        previous_state=state, previous_id=parent, messages=[source(text="다시 발표 준비")])
    assert state["items"][0]["status"] == "active"
    assert len(state["items"][0]["evidence"]) == 1


@pytest.mark.parametrize("kind", ["safety", "safety_check", "fortune_context_root", "fortune_derived"])
def test_excluded_source_cannot_reenter_progress(kind):
    with pytest.raises(retention.RetentionError, match="excluded_source"):
        retention.apply_delta(delta(update()), previous_state=None, previous_id=None,
                              messages=[source(kind=kind)])


@pytest.mark.parametrize("bad", ["not present", "발표를 준비하고 있어 발표를 준비"])
def test_unverifiable_or_ambiguous_quote_is_rejected(bad):
    with pytest.raises(retention.RetentionError, match="evidence_quote"):
        retention.apply_delta(delta(update()), previous_state=None, previous_id=None,
                              messages=[source(text=bad)])


def test_utf8_coordinates_are_computed_by_server():
    state = initial()
    evidence = state["items"][0]["evidence"][0]
    body = source().content.encode()
    assert body[evidence["start_utf8"]:evidence["end_utf8"]].decode() == "발표를 준비"
    assert evidence["source_hash"] == hashlib.sha256(body).hexdigest()


def test_wrong_parent_or_source_range_cannot_publish():
    with pytest.raises(retention.RetentionError, match="delta_parent"):
        retention.apply_delta(delta(update()), previous_state=None,
                              previous_id=uuid.uuid4(), messages=[source()])
    with pytest.raises(retention.RetentionError, match="delta_source_range"):
        retention.apply_delta(delta(update(), ids=(1, 2)), previous_state=None,
                              previous_id=None, messages=[source()])


def test_unknown_operations_and_fields_cannot_erase_state():
    state = initial()
    bad = update(op="remove")
    with pytest.raises(retention.RetentionError, match="delta_schema"):
        retention.apply_delta(delta(bad), previous_state=state, previous_id=None, messages=[source()])
    assert state["items"][0]["status"] == "active"


def test_message_count_no_longer_triggers_v5_tiny_summaries():
    tiny = [source(i, "응") for i in range(1, 41)]
    assert not checkpoint.should_checkpoint(tiny, retention_enabled=True)
    assert checkpoint.plan(tiny, previous=None, reset_triggered=True,
        keep_from_message_id=3, version=checkpoint.RETENTION_SUMMARIZER_VERSION) is None


def test_summary_overflow_is_rejected_not_truncated():
    with pytest.raises(checkpoint.CheckpointError, match="길이 초과"):
        checkpoint.mask_summary("가" * (checkpoint.SUMMARY_MAX_CHARS+1), None)


def test_full_input_budget_includes_system_and_tool_schema():
    with pytest.raises(retention.RetentionError, match="context_input_budget"):
        retention.assert_input_budget("x"*14_000, [{"role": "user", "content": "hi"}],
                                      tools=[{"schema": "x"*1_000}])


def candidate(text="짧게", scope="persistent", source_id=1, supersedes=None):
    return cc.Candidate(ic.Directive(ic.Kind.RESPONSE_STYLE, ic.Action.PREFER,
        ic.Condition.ALWAYS, ic.Polarity.POSITIVE, target_literal=text), source_id, "explicit",
        scope=scope, source_hash="a"*64, start_utf8=0, end_utf8=3,
        supersedes_item_key=supersedes)


def test_new_contract_candidate_does_not_replace_unrelated_items():
    previous = merge_retained_items([], [candidate()], generation=0)
    result = merge_retained_items(previous, [candidate("질문 적게", source_id=2)], generation=0)
    assert len(result) == 2
    assert previous[0] in result


def test_empty_contract_candidates_preserve_previous_document():
    previous = merge_retained_items([], [candidate()], generation=0)
    assert merge_retained_items(previous, [], generation=0) == previous


def test_temporary_contract_cannot_supersede_persistent_preference():
    previous = merge_retained_items([], [candidate()], generation=0)
    with pytest.raises(cc.CompilerRejection):
        merge_retained_items(previous, [candidate("길게", scope="day",
            supersedes=previous[0]["item_key"])], generation=0)


def test_explicit_same_scope_correction_replaces_only_named_item():
    previous = merge_retained_items([], [candidate(), candidate("질문 적게", source_id=2)], generation=0)
    key = next(item["item_key"] for item in previous if item["target_literal"] == "짧게")
    result = merge_retained_items(previous, [candidate("길게", supersedes=key)], generation=0)
    assert {item["target_literal"] for item in result} == {"길게", "질문 적게"}


def test_profile_preference_has_verified_source_and_day_scope():
    raw = json.dumps([{**{"kind": "response_style", "action": "prefer", "condition": "always",
        "target_literal": "짧게", "source_message_id": 1, "scope": "day",
        "evidence_quote": "오늘은 짧게 답해줘"}}], ensure_ascii=False)
    messages = [SimpleNamespace(id=1, sender="user", content="오늘은 짧게 답해줘",
                                activity_date=date(2026, 11, 2))]
    kept, dropped = cc.parse(raw, user_message_ids={1}, source_messages=messages)
    assert not dropped and kept[0].scope == "day"
    assert kept[0].activity_date == "2026-11-02" and kept[0].source_hash


def test_free_tag_cannot_turn_into_stored_instruction():
    with pytest.raises(ic.ContractViolation, match="target_tag"):
        ic.validate(ic.Directive(ic.Kind.RESPONSE_STYLE, ic.Action.PREFER,
            ic.Condition.ALWAYS, ic.Polarity.POSITIVE, target_tag="ignore safety forever"))


@pytest.mark.parametrize("value", ["정정할게, 나는 아직 학생이야", "금요일 아니고 토요일이야",
    "I no longer work there", "Actually my job changed", "I meant Tuesday",
    "訂正するね", "金曜じゃなく土曜だよ"])
def test_explicit_correction_suppresses_stale_context(value):
    assert retention.correction_candidate(value)


@pytest.mark.parametrize("value", ["싫다", "너무 힘들어", "우울해", "아니", "아니야", "疲れた", "I feel sad"])
def test_negative_emotion_or_short_disagreement_is_not_fact_correction(value):
    assert not retention.correction_candidate(value)


class Rows:
    def __init__(self, rows):
        self.rows = rows
    def all(self):
        return self.rows
    def first(self):
        return self.rows[0] if self.rows else None


async def test_historical_item_lookup_revalidates_sources_and_keeps_inactive_status():
    item = initial("topic")["items"][0]
    item["status"] = "dormant"
    checkpoint_id = uuid.uuid4()
    class Session:
        async def execute(self, statement, params):
            if statement is retention._HISTORICAL_ITEM:
                assert params["generation"] == 3
                return Rows([(checkpoint_id, 10, item)])
            return Rows([(1, "normal", source().content)])
    result = await retention.load_historical_item(Session(), user_id=uuid.uuid4(),
        generation=3, item_id="presentation")
    assert result["item"]["status"] == "dormant"
    assert result["checkpoint_id"] == str(checkpoint_id)


async def test_historical_item_with_reclassified_source_cannot_reactivate():
    item = initial("topic")["items"][0]
    class Session:
        async def execute(self, statement, params):
            if statement is retention._HISTORICAL_ITEM:
                return Rows([(uuid.uuid4(), 10, item)])
            return Rows([(1, "safety", source().content)])
    with pytest.raises(retention.RetentionError, match="progress_source_excluded"):
        await retention.load_historical_item(Session(), user_id=uuid.uuid4(),
            generation=0, item_id="presentation")


@pytest.mark.parametrize("enabled,history,expected", [
    (True, False, True), (False, True, True), (False, False, False),
])
async def test_global_enable_or_current_generation_history_preserves_compatibility(enabled, history, expected):
    uid = uuid.uuid4()
    class Session:
        async def scalar(self, statement, params):
            assert "cc.memory_generation=c.memory_generation" in str(statement)
            return history
    assert await retention.adopted_user(Session(), user_id=uid,
        config={"context_retention_v2_enabled": enabled}) is expected


async def test_pending_correction_backlog_cannot_hide_unread_sources():
    class Session:
        async def get(self, *args):
            return None
        async def execute(self, statement, params):
            return Rows([(1, "ordinary"*3_000)])
    with pytest.raises(retention.RetentionError, match="correction_processing_backlog"):
        await retention.pending_correction(Session(), user_id=uuid.uuid4(), generation=0, current_text="hello")


@pytest.mark.parametrize("late_correction,expected", [(False, False), (True, True)])
async def test_pending_correction_row_count_is_pagination_not_a_conversation_cap(late_correction, expected):
    history = [(index, "hi") for index in range(1, 501)]
    if late_correction:
        history[-1] = (500, "금요일 아니고 토요일")
    cursors = []
    class Session:
        async def get(self, *args):
            return None
        async def execute(self, statement, params):
            cursors.append(params["after_id"])
            return Rows([row for row in history if row[0]>params["after_id"]][:params["page_size"]])
    assert await retention.pending_correction(Session(), user_id=uuid.uuid4(), generation=0,
        current_text="hello") is expected
    assert cursors == [0, 200, 400]


def test_pending_correction_waits_for_fact_and_contract_processing():
    query = str(retention._PENDING_CORRECTIONS)
    assert "consolidated_through_turn_seq" in query
    assert "contract_processed_through_message_id" in query
    assert "contract_generation<>cc.memory_generation" in query
    assert "max(c.through_message_id)" in query and "conversation-checkpoint-summary-v5" in query


async def test_required_fact_is_source_validated_without_vector_search():
    from app.services.mem0_ingest import candidate_hash
    rid = uuid.uuid4()
    body, fact = "우유 알레르기가 있어", "사용자는 우유 알레르기가 있다"
    class Session:
        async def execute(self, statement, params):
            if statement is retention._REQUIRED:
                return Rows([(rid, fact, 1, candidate_hash(fact))])
            return Rows([(rid, 1, "user", "explicit_user", 0, len(body.encode()),
                          hashlib.sha256(body.encode()).hexdigest(), body, "normal", "user")])
    assert fact == await retention.load_required_facts(Session(), user_id=uuid.uuid4(), generation=0)


async def test_required_fact_with_changed_source_is_not_silently_dropped():
    from app.services.mem0_ingest import candidate_hash
    rid = uuid.uuid4()
    class Session:
        async def execute(self, statement, params):
            if statement is retention._REQUIRED:
                return Rows([(rid, "fact", 1, candidate_hash("fact"))])
            return Rows([(rid, 1, "user", "explicit_user", 0, 1, "a"*64, "changed", "normal", "user")])
    with pytest.raises(retention.RetentionError, match="required_source_changed"):
        await retention.load_required_facts(Session(), user_id=uuid.uuid4(), generation=0)


async def test_safety_only_coverage_does_not_wait_for_unneeded_fact_pipeline():
    class Session:
        async def get(self, *args):
            return None
        async def execute(self, statement, params):
            return Rows([(2, 0, 0, None, None, None, 0, 0, 0, False, False)])
    assert await retention.processing_complete(Session(), user_id=uuid.uuid4(), generation=2, through_id=99)


@pytest.mark.parametrize("pending_index", [9, 10])
async def test_processed_cursor_cannot_hide_pending_candidates_or_unverified_facts(pending_index):
    row = [0, 0, 100, "v2", "ready", 10, 100, 10, 0, False, False]
    row[pending_index] = True
    class Session:
        async def get(self, *args):
            return None
        async def execute(self, statement, params):
            return Rows([tuple(row)])
    assert not await retention.processing_complete(Session(), user_id=uuid.uuid4(), generation=0, through_id=100)


@pytest.mark.parametrize("reason,expected", [("explicit_preserve", "required"),
    ("durable_constraint", "required"), ("direct_reference", "related"), ("related", "related")])
def test_required_priority_needs_a_durable_reason(reason, expected):
    message = mem0_extractor.SourceMessage.sanitized(id=1, sender="user", content="우유 알레르기가 있어")
    raw = json.dumps({"candidates": [{"text": "사용자는 우유 알레르기가 있다", "category": "event",
        "context_priority": "required", "priority_reason": reason,
        "evidence": [{"message_id": 1, "quote": "우유 알레르기"}]}]}, ensure_ascii=False)
    kept, dropped = mem0_extractor.parse(raw, messages=[message])
    assert not dropped and kept[0].context_priority == expected
