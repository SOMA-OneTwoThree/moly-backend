"""Packed summary workers preserve source, durable call budgets, and legacy behavior."""
import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import checkpoint, context_retention, llm
from tests import test_retention_worker as legacy_tests
from worker import checkpoint_jobs as worker
from worker.consumer import JobCancelled, JobFatal, JobRetry

environment = legacy_tests.environment
make_job = legacy_tests.make_job
_REAL_PROGRESS = checkpoint.summarize_progress


@pytest.fixture
def packed(environment, monkeypatch):
    state, session = environment
    state["messages"] = [checkpoint.SourceMessage(1, "user", "normal", "가나다🙂" * 500)]
    original = make_job(state["messages"])
    original = replace(original, payload={**original.payload, "packing_version": "packed-v1"})
    state["durable"] = copy.deepcopy(original.payload)
    state["controls"] = []

    async def persist(job, before, after):
        assert before == state["durable"]
        assert job.id == original.id or state.get("resumed")
        state["durable"] = copy.deepcopy(after)
        state["controls"].append(copy.deepcopy(after))

    monkeypatch.setattr(worker, "_persist_packing_control", persist)
    monkeypatch.setattr(checkpoint, "summarize_progress", _REAL_PROGRESS)
    state["provider"] = []

    async def generate(system, convo, **kwargs):
        body = json.loads(convo[0]["content"])
        state["provider"].append(body)
        action = state.get("actions", [])
        reason = action.pop(0) if action else "stop"
        if isinstance(reason, Exception):
            raise reason
        text = "not parseable partial" if reason == "length" else json.dumps({
            "base_checkpoint_id": body["base_checkpoint_id"],
            "source_message_ids": body["source_message_ids"], "updates": []})
        return llm.LLMResult(text, 10, 5, model="gpt-6-luna", finish_reason=reason)

    monkeypatch.setattr(llm, "generate", generate)
    return state, session, original


def next_job(job, state):
    queued = state["enqueued"][-1]
    state["durable"] = copy.deepcopy(queued["payload"])
    state["resumed"] = True
    return replace(job, payload=queued["payload"], attempt=1,
                   max_attempts=queued.get("max_attempts", 3))


async def test_packed_final_draft_has_complete_proof_and_publishes_without_another_model(packed):
    state, session, job = packed
    result = await worker.handle_checkpoint(job)
    assert len(state["provider"]) == 1
    assert state["controls"][0]["model_attempts_remaining"] == 2
    await result.apply_domain(session)
    publish = next_job(job, state)
    assert publish.payload["packing_version"] == "packed-v1"
    assert publish.payload["completed_proof"]["completed_chunks"] > 1
    result = await worker.handle_checkpoint(publish)
    await result.apply_domain(session)
    assert len(state["provider"]) == 1
    assert len(state["inserts"]) == 1


async def test_enlarged_length_never_parses_or_advances_and_uses_exact_cap_one_child(packed):
    state, session, job = packed
    state["actions"] = ["length"]
    result = await worker.handle_checkpoint(job)
    assert result.result_detail["billable"] == 40
    assert state["controls"][-1]["packing_chunk_cap"] == 1
    assert state["controls"][-1]["model_attempts_remaining"] == 2
    await result.apply_domain(session)
    child = next_job(job, state)
    assert child.max_attempts == 2 and child.payload["packing_chunk_cap"] == 1
    for key in ["source_hash", "source_message_ids", "previous_checkpoint_id"]:
        assert child.payload[key] == job.payload[key]
    assert "progress_state" not in child.payload and "completed_state" not in child.payload
    assert not state["inserts"]
    assert ":packed-v1:0:1" in state["enqueued"][-1]["dedup_key"]


async def test_first_length_off_then_new_row_on_resumes_cap_and_unspent_budget(packed):
    state, session, job = packed
    state["actions"] = ["length"]
    result = await worker.handle_checkpoint(job)
    state["enabled"] = False
    await result.apply_domain(session)
    assert not state["enqueued"] and not state["inserts"]
    resumed = replace(job, payload=copy.deepcopy(state["durable"]), attempt=1, max_attempts=3)
    state["resumed"] = True
    state["enabled"] = True
    result = await worker.handle_checkpoint(resumed)
    assert state["controls"][-1]["model_attempts_remaining"] == 1
    await result.apply_domain(session)
    child = next_job(resumed, state)
    assert child.payload["packing_chunk_cap"] == 1
    assert child.payload["model_attempts_remaining"] == 2 and child.max_attempts == 2


async def test_provider_failure_then_off_on_new_row_never_refills_numeric_budget(packed):
    state, session, job = packed
    job = replace(job, payload={**job.payload, "packing_chunk_cap": 1, "model_attempts_remaining": 2})
    state["durable"] = copy.deepcopy(job.payload)
    state["actions"] = [RuntimeError("synthetic")]
    with pytest.raises(JobRetry, match="summary_llm_failed"):
        await worker.handle_checkpoint(job)
    assert state["durable"]["model_attempts_remaining"] == 1
    resumed = replace(job, payload=copy.deepcopy(state["durable"]), attempt=1, max_attempts=3)
    state["resumed"] = True
    state["actions"] = [RuntimeError("synthetic")]
    with pytest.raises(JobRetry):
        await worker.handle_checkpoint(resumed)
    assert state["durable"]["model_attempts_remaining"] == 0
    exhausted = replace(resumed, payload=copy.deepcopy(state["durable"]), attempt=1)
    count = len(state["provider"])
    with pytest.raises(JobFatal, match="attempts_exhausted"):
        await worker.handle_checkpoint(exhausted)
    assert len(state["provider"]) == count


async def test_successful_atomic_pages_keep_failure_budget_and_never_reenlarge(packed):
    state, session, job = packed
    job = replace(job, payload={**job.payload, "packing_chunk_cap": 1, "model_attempts_remaining": 2})
    state["durable"] = copy.deepcopy(job.payload)
    for _ in range(20):
        result = await worker.handle_checkpoint(job)
        await result.apply_domain(session)
        job = next_job(job, state)
        assert job.payload["packing_chunk_cap"] == 1
        if job.payload.get("mode") == "publish_retention":
            break
        assert job.payload["model_attempts_remaining"] == 2
        assert job.max_attempts == 2
    else:
        pytest.fail("did not complete every original atom")
    assert len(state["provider"]) > 2
    assert all(row["model_attempts_remaining"] == 1 for row in state["controls"])


async def test_single_atom_length_is_regular_retry_and_no_fallback_child(packed):
    state, session, job = packed
    job = replace(job, payload={**job.payload, "packing_chunk_cap": 1})
    state["durable"] = copy.deepcopy(job.payload)
    state["actions"] = ["length"]
    with pytest.raises(JobRetry):
        await worker.handle_checkpoint(job)
    assert state["durable"]["model_attempts_remaining"] == 2
    assert not state["enqueued"]


async def test_last_available_enlarged_length_has_no_child(packed):
    state, session, job = packed
    job = replace(job, payload={**job.payload, "model_attempts_remaining": 1})
    state["durable"] = copy.deepcopy(job.payload)
    state["actions"] = ["length"]
    with pytest.raises(JobFatal, match="attempts_exhausted"):
        await worker.handle_checkpoint(job)
    assert state["durable"]["packing_chunk_cap"] == 1
    assert state["durable"]["model_attempts_remaining"] == 0
    assert not state["enqueued"]


async def test_off_after_successful_call_keeps_precharge_and_creates_no_page(packed):
    state, session, job = packed
    job = replace(job, payload={**job.payload, "packing_chunk_cap": 1})
    state["durable"] = copy.deepcopy(job.payload)
    result = await worker.handle_checkpoint(job)
    state["enabled"] = False
    await result.apply_domain(session)
    assert state["durable"]["model_attempts_remaining"] == 2
    assert not state["enqueued"]


async def test_fallback_cannot_create_child_after_source_change(packed):
    state, session, job = packed
    state["actions"] = ["length"]
    result = await worker.handle_checkpoint(job)
    state["messages"] = [replace(state["messages"][0], content="changed")]
    await result.apply_domain(session)
    assert not state["enqueued"] and not state["inserts"]


async def test_input_failure_does_not_reserve_or_call(packed, monkeypatch):
    state, session, job = packed
    def reject(*args, **kwargs):
        raise context_retention.RetentionError("context_input_budget")
    monkeypatch.setattr(context_retention, "assert_input_budget", reject)
    with pytest.raises(JobRetry):
        await worker.handle_checkpoint(job)
    assert not state["controls"] and not state["provider"]


@pytest.mark.parametrize("fields", [
    {"packing_version": "other"}, {"packing_chunk_cap": True},
    {"packing_chunk_cap": 0}, {"model_attempts_remaining": False},
    {"model_attempts_remaining": -1}, {"model_attempts_remaining": "2"},
])
async def test_invalid_packing_controls_fail_before_model(packed, fields):
    state, session, job = packed
    with pytest.raises(JobFatal):
        await worker.handle_checkpoint(replace(job, payload={**job.payload, **fields}))
    assert not state["provider"]


async def test_legacy_has_no_new_kwargs_or_control_reservation(environment, monkeypatch):
    state, session = environment
    model = AsyncMock(return_value=("", {**context_retention.empty_state(), "sequence": 1},
                                   SimpleNamespace(billable=1)))
    monkeypatch.setattr(checkpoint, "summarize_progress", model)
    persist = AsyncMock()
    monkeypatch.setattr(worker, "_persist_packing_control", persist)
    await worker.handle_checkpoint(make_job(state["messages"]))
    assert "packing_version" not in model.call_args.kwargs
    assert "before_call" not in model.call_args.kwargs
    persist.assert_not_awaited()


async def test_control_sql_uses_exact_payload_and_original_lease_generation_privacy(environment, monkeypatch):
    state, session = environment
    original = make_job(state["messages"])
    job = replace(original, payload={**original.payload, "packing_version": "packed-v1"})
    calls = []
    async def execute(sql, params):
        calls.append((str(sql), params))
        return SimpleNamespace(first=lambda: (job.id,))
    session.execute = execute
    after = {**job.payload, "model_attempts_remaining": 2}
    await worker._persist_packing_control(job, job.payload, after)
    sql, params = calls[0]
    for condition in ["lease_owner=:owner", "lease_token=:token", "lease_until>now()",
                      "j.state='running'", "j.payload=CAST(:before AS jsonb)",
                      "payload_redacted_at IS NULL", "payload_expires_at>now()",
                      "c.memory_generation=:generation"]:
        assert condition in sql
    assert json.loads(params["before"]) == job.payload
    assert json.loads(params["payload"]) == after
    assert params["payload_hash"] == hashlib.sha256(params["payload"].encode()).hexdigest()
    assert params["owner"] == job.lease_owner and params["token"] == job.lease_token
    assert session.commits == 1


@pytest.mark.parametrize("mismatch", ["lease", "privacy"])
async def test_control_snapshot_or_privacy_loss_never_commits(environment, monkeypatch, mismatch):
    state, session = environment
    original = make_job(state["messages"])
    job = replace(original, payload={**original.payload, "packing_version": "packed-v1"})
    async def execute(*args):
        return SimpleNamespace(first=lambda: None)
    session.execute = execute
    if mismatch == "privacy":
        monkeypatch.setattr(context_retention, "privacy_epoch_matches", AsyncMock(return_value=False))
    with pytest.raises(JobCancelled):
        await worker._persist_packing_control(job, job.payload,
            {**job.payload, "model_attempts_remaining": 2})
    assert session.commits == 0


async def test_length_on_later_group_preserves_exact_previous_state_position_and_proof(packed):
    state, session, job = packed
    state["messages"] = [replace(state["messages"][0], content="가나다🙂" * 3000)]
    original = make_job(state["messages"])
    job = replace(original, payload={**original.payload, "packing_version": "packed-v1"})
    state["resumed"] = True
    state["durable"] = copy.deepcopy(job.payload)
    result = await worker.handle_checkpoint(job)
    await result.apply_domain(session)
    job = next_job(job, state)
    assert job.payload["progress_next_chunk"] > 0
    saved = copy.deepcopy(job.payload)
    state["actions"] = ["length"]
    result = await worker.handle_checkpoint(job)
    await result.apply_domain(session)
    fallback = next_job(job, state)
    for key in ["progress_next_chunk", "progress_manifest", "progress_state", "progress_page_proof",
                "source_hash", "source_message_ids"]:
        assert fallback.payload[key] == saved[key]
    assert fallback.payload["packing_chunk_cap"] == 1
    assert fallback.payload["model_attempts_remaining"] == saved["model_attempts_remaining"] - 1


async def test_packed_policy_only_range_completes_without_call_or_reservation(packed):
    state, session, job = packed
    state["messages"] = [checkpoint.SourceMessage(1, "user", "safety", "synthetic")]
    original = make_job(state["messages"])
    job = replace(original, payload={**original.payload, "packing_version": "packed-v1"})
    state["durable"] = copy.deepcopy(job.payload)
    result = await worker.handle_checkpoint(job)
    await result.apply_domain(session)
    assert not state["provider"] and not state["controls"]
    publish = next_job(job, state)
    result = await worker.handle_checkpoint(publish)
    await result.apply_domain(session)
    assert len(state["inserts"]) == 1 and not state["provider"]


async def test_control_helper_cannot_store_content_or_rebind_source(environment):
    state, session = environment
    original = make_job(state["messages"])
    job = replace(original, payload={**original.payload, "packing_version": "packed-v1"})
    with pytest.raises(JobFatal, match="control_invalid"):
        await worker._persist_packing_control(job, job.payload,
            {**job.payload, "source_hash": "different", "model_attempts_remaining": 2})
    assert session.commits == 0


@pytest.mark.parametrize("changed", ["generation", "privacy", "parent"])
async def test_fallback_child_keeps_final_generation_privacy_parent_fences(packed, monkeypatch, changed):
    state, session, job = packed
    state["actions"] = ["length"]
    result = await worker.handle_checkpoint(job)
    if changed == "generation":
        monkeypatch.setattr(worker.checkpoint_repo, "read_memory_generation", AsyncMock(return_value=1))
    elif changed == "privacy":
        monkeypatch.setattr(context_retention, "privacy_epoch_matches", AsyncMock(return_value=False))
    else:
        state["current"] = checkpoint.Checkpoint(job.id, 1, "",
            checkpoint.RETENTION_SUMMARIZER_VERSION, "changed", context_retention.empty_state(), {})
    await result.apply_domain(session)
    assert not state["enqueued"] and not state["inserts"]


@pytest.mark.parametrize("failure", [JobCancelled("lost"), worker.JobDeferred("busy"), JobFatal("invalid")])
async def test_call_reservation_cancellation_or_busy_never_becomes_provider_retry(packed, monkeypatch, failure):
    state, session, job = packed
    monkeypatch.setattr(worker, "_persist_packing_control", AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await worker.handle_checkpoint(job)
    assert not state["provider"]
