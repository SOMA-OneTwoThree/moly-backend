"""초장문 배경 작업은 모든 UTF-8 구간을 읽은 뒤에만 완료한다. 외부 호출 없음."""
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.services import checkpoint, context_retention as retention, jobs, llm
from worker import contract_jobs as worker
from worker.consumer import JobCancelled


def message(body="한글🙂" * 1400):
    return checkpoint.SourceMessage(1, "user", "normal", body)


def job(payload=None):
    return jobs.ClaimedJob(uuid.uuid4(), "content", "contract_compile", uuid.uuid4(),
        "budget-test", payload or {}, 1, 3, "worker", uuid.uuid4(),
        datetime.now(timezone.utc) + timedelta(minutes=1), None)


def test_utf8_segments_cover_original_without_loss_or_rebased_evidence():
    original = message()
    pieces = checkpoint.source_segments(original)
    assert len(pieces) > 1
    assert "".join(p.content for p in pieces) == original.content
    cursor = 0
    for part in pieces:
        assert part.id == original.id and part.start_utf8 == cursor
        assert part.end_utf8 - part.start_utf8 <= checkpoint.BACKGROUND_SEGMENT_BYTES
        assert original.content.encode()[part.start_utf8:part.end_utf8].decode() == part.content
        assert part.source_hash == hashlib.sha256(original.content.encode()).hexdigest()
        cursor = part.end_utf8
    assert cursor == len(original.content.encode())


@pytest.mark.parametrize("changes", [dict(start_utf8=3), dict(end_utf8=4), dict(source_hash="bad")])
def test_invalid_segment_metadata_is_not_silently_repaired(changes):
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.source_segments(replace(message("가나다"), **changes))


async def test_progress_continuation_checks_each_whole_input_and_advances_once(monkeypatch):
    calls, spans = [], []

    async def generate(system, messages, **kwargs):
        retention.assert_input_budget(system, messages, output_reserve=kwargs["max_tokens"])
        body = json.loads(messages[0]["content"])
        calls.append(body)
        source = body["messages"][0]
        # 둘째 구간에 있는 원문을 근거로 추가한다. 첫 구간 지문으로 바꾸면 안 된다.
        updates = []
        if len(calls) == 2:
            updates = [{"op": "add", "item_id": "current-topic", "kind": "topic",
                "text": "진행 중인 주제", "evidence": [{"message_id": 1, "quote": "유일한표지"}]}]
            assert source["content"].startswith("유일한표지")
            spans.append("유일한표지")
        return llm.LLMResult(json.dumps({"base_checkpoint_id": None,
            "source_message_ids": [1], "updates": updates}, ensure_ascii=False), 11, 3,
            finish_reason="stop")

    monkeypatch.setattr(llm, "generate", generate)
    original = message("가" * 682 + "유일한표지" + "나" * 3000)
    state, start, manifest = None, 0, None
    while True:
        try:
            summary, result, call = await checkpoint.summarize_progress(messages=[original], previous=None,
                language="ko", nickname=None, continuation_state=state, start_chunk=start,
                continuation_manifest=manifest)
            break
        except checkpoint.ProgressContinuation as continuation:
            assert continuation.state["sequence"] == 0
            assert continuation.billable > 0
            state, start, manifest = continuation.state, continuation.next_chunk, continuation.manifest
    assert len(calls) == len(checkpoint.source_segments(original))
    assert result["sequence"] == 1 and result["items"][0]["idle_checkpoints"] == 0
    evidence = result["items"][0]["evidence"][0]
    assert evidence["start_utf8"] == checkpoint.source_segments(original)[1].start_utf8
    assert original.content.encode()[evidence["start_utf8"]:evidence["end_utf8"]].decode() == spans[0]
    assert evidence["source_hash"] == hashlib.sha256(original.content.encode()).hexdigest()
    assert call.input_tokens == 11 and summary


async def test_changed_progress_source_cannot_resume_old_manifest(monkeypatch):
    async def generate(system, messages, **kwargs):
        return llm.LLMResult(json.dumps({"base_checkpoint_id": None,
            "source_message_ids": [1], "updates": []}), 1, 1, finish_reason="stop")
    monkeypatch.setattr(llm, "generate", generate)
    with pytest.raises(checkpoint.ProgressContinuation) as caught:
        await checkpoint.summarize_progress(messages=[message()], previous=None,
            language="ko", nickname=None)
    continuation = caught.value
    with pytest.raises(checkpoint.CheckpointError, match="manifest"):
        await checkpoint.summarize_progress(messages=[message("다른 원문" * 2000)], previous=None,
            language="ko", nickname=None, start_chunk=continuation.next_chunk,
            continuation_state=continuation.state, continuation_manifest=continuation.manifest)


async def test_progress_rejects_whole_input_over_budget_before_model(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("예산을 넘는 모델 호출")
    monkeypatch.setattr(llm, "generate", forbidden)
    monkeypatch.setattr(retention, "build_delta_system", lambda language: "x" * 20_000)
    with pytest.raises(checkpoint.CheckpointError, match="budget"):
        await checkpoint.summarize_progress(messages=[message("짧은 원문")], previous=None,
            language="ko", nickname=None)


def test_contract_batches_preserve_every_byte_and_bound_each_full_request():
    original = worker._Msg(3, "user", "🙂한글" * 2000)
    index, offset, pieces = 0, 0, []
    while index == 0:
        batch = worker.contract_batch([original], previous=[], system=worker.cc.RETENTION_SYSTEM,
                                      start_index=index, start_offset=offset)
        retention.assert_input_budget(worker.cc.RETENTION_SYSTEM,
            worker._model_input(batch.messages, []), output_reserve=2000)
        part = batch.messages[0]
        assert part.start_utf8 == offset and part.source_hash == hashlib.sha256(original.content.encode()).hexdigest()
        assert part.end_utf8 > offset
        pieces.append(part.content)
        index, offset = batch.next_index, batch.next_offset
    assert index == 1 and offset == 0 and "".join(pieces) == original.content


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)
    def all(self):
        return self.rows
    def first(self):
        return self.rows[0] if self.rows else None


@pytest.fixture
def contract_environment(monkeypatch):
    state = {"body": "This conversation, use short replies. " + "가나다🙂" * 1000,
             "cursor": 0, "published": [], "queued": [], "docs": {}, "calls": 0,
             "adopted": True, "enabled": True, "message_id": 1}

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def execute(self, statement, params):
            if statement in (worker._RETENTION_STATE, worker._RETENTION_STATE_LOCK):
                return Result([(0, 0, state["cursor"])])
            if statement is worker._RETENTION_MESSAGES:
                mid = state["message_id"]
                return Result([(mid, "user", state["body"], None)] if params["after"] < mid else [])
            if statement in (worker._MESSAGES, worker._LEGACY_SOURCE):
                return Result([(1, "user", state["body"])])
            if statement is worker._PROFILE:
                return Result([(None, "en")])
            if statement is worker.contract_repo._VALID_SOURCES:
                return Result([(1, state["body"])])
            if statement is worker._RETENTION_ADVANCE:
                state["cursor"] = params["through"]
            return Result()
        async def scalar(self, statement, params):
            if statement is worker._NEXT_VERSION:
                return len(state["docs"]) + 1
            if statement in (worker._INSERT, worker._RETENTION_INSERT):
                cid = uuid.uuid4()
                state["docs"][cid] = json.loads(params["doc"])
                return cid
            return 0

    session = Session()
    monkeypatch.setattr(worker, "get_sessionmaker", lambda: lambda: session)
    async def adopted(*args, **kwargs):
        return state["adopted"]
    async def enabled(*args, **kwargs):
        return state["enabled"]
    async def retained(*args, **kwargs):
        return state["published"]
    async def same(*args, **kwargs):
        return False
    async def publish(session, *, contract_id, **kwargs):
        state["published"] = state["docs"][contract_id]
    async def enqueue(session, **kwargs):
        state["queued"].append(kwargs)
    async def generate(system, messages, **kwargs):
        retention.assert_input_budget(system, messages, output_reserve=kwargs["max_tokens"])
        state["calls"] += 1
        candidate = {"kind": "response_style", "action": "prefer", "condition": "always",
            "target_literal": "short replies", "source_message_id": 1,
            "scope": "conversation", "evidence_quote": "use short replies"}
        candidates = [candidate] if "use short replies" in messages[0]["content"] else []
        return llm.LLMResult(json.dumps(candidates), 5, 3, finish_reason="stop")
    monkeypatch.setattr(retention, "adopted_user", adopted)
    monkeypatch.setattr(retention, "enabled_now", enabled)
    monkeypatch.setattr(worker.contract_repo, "retained_document", retained)
    monkeypatch.setattr(worker.contract_repo, "has_same_document", same)
    monkeypatch.setattr(worker.contract_repo, "publish", publish)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(llm, "generate", generate)
    return state, session


async def test_retention_contract_only_publishes_after_whole_source(contract_environment):
    state, session = contract_environment
    payload = {"retention_enabled": True, "memory_generation": 0, "through_message_id": 1}
    iterations = 0
    while True:
        result = await worker.handle_contract_compile(job(payload))
        await result.apply_domain(session)
        iterations += 1
        if state["cursor"] == 1:
            break
        assert state["published"] == [] and state["docs"] == {}
        payload = state["queued"].pop()["payload"]
        assert payload["partial_offset"] > 0
    assert iterations > 1 and state["calls"] == iterations
    item = state["published"][0]
    assert item["scope_start_message_id"] == 1 and item["status"] == "active"
    assert item["source_hash"] == hashlib.sha256(state["body"].encode()).hexdigest()


async def test_legacy_large_contract_uses_durable_parts_without_early_publish(contract_environment):
    state, session = contract_environment
    state["adopted"] = False
    payload = {}
    while True:
        result = await worker.handle_contract_compile(job(payload))
        await result.apply_domain(session)
        if result.result_code != "continued":
            break
        assert not state["docs"] and not state["published"]
        payload = state["queued"].pop()["payload"]
    assert state["calls"] > 1 and state["published"][0]["source_message_id"] == 1


async def test_legacy_continuation_rejects_changed_source_before_model(contract_environment):
    state, session = contract_environment
    state["adopted"] = False
    result = await worker.handle_contract_compile(job())
    await result.apply_domain(session)
    payload = state["queued"].pop()["payload"]
    state["body"] += "changed"
    with pytest.raises(JobCancelled, match="source_changed"):
        await worker.handle_contract_compile(job(payload))
    assert state["calls"] == 1


async def test_legacy_job_cannot_overwrite_adopted_v5_document(contract_environment):
    state, session = contract_environment
    state["adopted"] = False
    state["body"] = "use short replies"
    result = await worker.handle_contract_compile(job())
    state["adopted"] = True
    state["enabled"] = False  # 채택 뒤 잠깐 꺼도 구형 작업이 v5를 덮으면 안 된다.
    await result.apply_domain(session)
    assert not state["docs"]


async def test_retention_toggle_off_after_model_cannot_publish_or_advance(contract_environment):
    state, session = contract_environment
    state["body"] = "use short replies"
    result = await worker.handle_contract_compile(job({"retention_enabled": True,
        "memory_generation": 0, "through_message_id": 1}))
    state["enabled"] = False
    await result.apply_domain(session)
    assert not state["docs"] and not state["queued"] and state["cursor"] == 0


async def test_retention_changed_source_restarts_current_range_without_old_draft(contract_environment):
    state, session = contract_environment
    result = await worker.handle_contract_compile(job({"retention_enabled": True,
        "memory_generation": 0, "through_message_id": 1}))
    state["body"] += "source changed while model was running"
    await result.apply_domain(session)
    assert not state["docs"] and state["cursor"] == 0
    payload = state["queued"][0]["payload"]
    assert payload == {"retention_enabled": True, "memory_generation": 0, "through_message_id": 1}


async def test_retention_partial_hash_mismatch_cannot_call_model(contract_environment):
    state, session = contract_environment
    result = await worker.handle_contract_compile(job({"retention_enabled": True,
        "memory_generation": 0, "through_message_id": 1}))
    await result.apply_domain(session)
    payload = state["queued"].pop()["payload"]
    state["body"] += "changed"
    with pytest.raises(JobCancelled, match="partial_source_changed"):
        await worker.handle_contract_compile(job(payload))
    assert state["calls"] == 1 and state["cursor"] == 0


@pytest.mark.parametrize("body", ["いつもの口調に戻して。", "그 말투 그만 써. 그 말투 그만 써."])
async def test_worker_persists_explicit_end_when_model_returns_empty(contract_environment, body):
    state, session = contract_environment
    original = worker._Msg(1, "user", "This conversation, use short replies.")
    candidate = {"kind": "response_style", "action": "prefer", "condition": "always",
        "target_literal": "short replies", "source_message_id": 1,
        "scope": "conversation", "evidence_quote": "use short replies"}
    kept, dropped = worker.cc.parse(json.dumps([candidate]), user_message_ids={1}, source_messages=[original])
    assert not dropped
    state["published"] = worker.merge_retained_items([], kept, generation=0)
    state.update(body=body, cursor=1, message_id=2)
    result = await worker.handle_contract_compile(job({"retention_enabled": True,
        "memory_generation": 0, "through_message_id": 2}))
    await result.apply_domain(session)
    assert state["cursor"] == 2 and state["calls"] == 1
    assert state["published"][0]["status"] == "ended"
    assert state["published"][0]["scope_end"]["source_message_id"] == 2
