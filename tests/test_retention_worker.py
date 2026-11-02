"""구조화 요약 선행 작업 대기와 정책 제외, 원문 변경 경계."""
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import checkpoint, checkpoint_repo, context_retention, jobs
from worker import checkpoint_jobs
from worker.consumer import JobDeferred

_REAL_SUMMARIZE_PROGRESS = checkpoint.summarize_progress


def make_job(messages):
    plan = checkpoint.CheckpointPlan(messages[-1].id, tuple(m.id for m in messages),
        checkpoint.source_hash(previous=None, messages=messages,
            version=checkpoint.RETENTION_SUMMARIZER_VERSION), None, None,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION)
    return jobs.ClaimedJob(uuid.uuid4(), "content", "conversation_checkpoint", uuid.uuid4(),
        "test-retention", plan.payload(), 1, 3, "worker", uuid.uuid4(),
        datetime.now(timezone.utc)+timedelta(minutes=1), None)


class Session:
    def __init__(self):
        self.commits = self.rollbacks = 0
        self.calls = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def scalar(self, *args, **kwargs):
        return 0
    async def commit(self):
        self.commits += 1
    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(settings, "context_checkpoint_enabled", True)
    state = {"messages": [checkpoint.SourceMessage(1, "user", "normal", "hello")],
             "complete": True, "models": 0, "inserts": [], "enqueued": [], "enabled": True,
             "adopted": True}
    session = Session()
    monkeypatch.setattr(checkpoint_jobs, "get_sessionmaker", lambda: lambda: session)
    async def config(*args):
        return {"context_retention_v2_enabled": True}
    async def latest(*args, **kwargs):
        return state.get("current")
    async def profile(*args):
        return (None, "en")
    async def generation(*args):
        return 0
    async def count(*args, **kwargs):
        return 0
    async def sources(*args, **kwargs):
        assert kwargs.get("include_excluded") is True
        rows = [m for m in state["messages"] if (kwargs.get("after_id") or 0) < m.id <= kwargs["through_id"]]
        return rows[:kwargs.get("max_rows") or len(rows)]
    async def complete(*args, **kwargs):
        return state["complete"]
    async def enabled(*args, **kwargs):
        return state["enabled"]
    async def adopted(*args, **kwargs):
        return state["adopted"]
    async def enqueue(*args, **kwargs):
        state["enqueued"].append(kwargs)
        return uuid.uuid4()
    async def contract_dependency(*args, **kwargs):
        return await enqueue(job_type="contract_compile", payload={"retention_enabled": True,
            "memory_generation": kwargs["generation"], "through_message_id": kwargs["through_id"]})
    async def insert(*args, **kwargs):
        state["inserts"].append(kwargs)
        return uuid.uuid4()
    async def model(*args, **kwargs):
        state["models"] += 1
        return "", context_retention.empty_state(), SimpleNamespace(billable=12)
    for owner, name, value in [
        (checkpoint_jobs.config_store, "get_config_values", config),
        (checkpoint_repo, "load_latest", latest), (checkpoint_repo, "load_user_state", profile),
        (checkpoint_repo, "read_memory_generation", generation), (checkpoint_repo, "count", count),
        (checkpoint_repo, "load_range", sources), (checkpoint_repo, "insert", insert),
        (context_retention, "processing_complete", complete), (jobs, "enqueue", enqueue),
        (context_retention, "enabled_now", enabled),
        (context_retention, "adopted_user", adopted),
        (context_retention, "enqueue_contract_processing", contract_dependency),
        (checkpoint, "summarize_progress", model),
    ]:
        monkeypatch.setattr(owner, name, value)
    return state, session


async def test_dependency_wait_does_not_call_summary_model(environment):
    state, session = environment
    state["complete"] = False
    with pytest.raises(JobDeferred, match="source_processing_pending"):
        await checkpoint_jobs.handle_checkpoint(make_job(state["messages"]))
    assert state["models"] == 0 and not state["inserts"]
    assert state["enqueued"][0]["job_type"] == "contract_compile"
    assert session.commits == 1


async def test_policy_only_range_does_not_call_model(environment):
    state, session = environment
    state["messages"] = [checkpoint.SourceMessage(1, "user", "safety", "synthetic safety"),
                         checkpoint.SourceMessage(2, "moly", "safety", "synthetic fixed")]
    result = await checkpoint_jobs.handle_checkpoint(make_job(state["messages"]))
    await result.apply_domain(session)
    assert state["models"] == 0 and result.result_detail["billable"] == 0
    assert state["inserts"][0]["validation_json"]["policy_excluded_ids"] == [1, 2]


async def test_source_change_after_model_cannot_publish(environment):
    state, session = environment
    result = await checkpoint_jobs.handle_checkpoint(make_job(state["messages"]))
    state["messages"] = [replace(state["messages"][0], content="changed")]
    await result.apply_domain(session)
    assert state["models"] == 1 and state["inserts"] == []


async def test_dependency_change_after_model_cannot_publish(environment):
    state, session = environment
    result = await checkpoint_jobs.handle_checkpoint(make_job(state["messages"]))
    state["complete"] = False
    await result.apply_domain(session)
    assert state["models"] == 1 and state["inserts"] == []


async def test_disabled_during_model_cannot_publish_or_advance(environment):
    state, session = environment
    result = await checkpoint_jobs.handle_checkpoint(make_job(state["messages"]))
    state["enabled"] = False
    await result.apply_domain(session)
    assert state["models"] == 1 and state["inserts"] == [] and state["enqueued"] == []


async def test_first_checkpoint_cannot_publish_after_ready_user_removal(environment):
    state, session = environment
    result = await checkpoint_jobs.handle_checkpoint(make_job(state["messages"]))
    state["adopted"] = False
    await result.apply_domain(session)
    assert not state["inserts"] and not state["enqueued"]


async def test_progress_continuation_is_fenced_and_never_published(environment, monkeypatch):
    state, session = environment
    class Continuation(Exception):
        state = context_retention.empty_state()
        next_chunk, manifest, billable = 1, "plan-hash", 17
    async def model(*args, **kwargs):
        assert kwargs["max_batches"] == 1
        raise Continuation()
    monkeypatch.setattr(checkpoint, "ProgressContinuation", Continuation, raising=False)
    monkeypatch.setattr(checkpoint, "summarize_progress", model)
    original = make_job(state["messages"])
    result = await checkpoint_jobs.handle_checkpoint(original)
    assert result.result_code == "checkpoint_progress_page" and result.result_detail["billable"] == 17
    await result.apply_domain(session)
    assert not state["inserts"] and len(state["enqueued"]) == 1
    payload = state["enqueued"][0]["payload"]
    assert payload["source_hash"] == original.payload["source_hash"]
    assert payload["source_message_ids"] == original.payload["source_message_ids"]
    assert payload["progress_next_chunk"] == 1 and payload["progress_manifest"] == "plan-hash"


async def test_progress_continuation_cannot_schedule_after_toggle_off(environment, monkeypatch):
    state, session = environment
    class Continuation(Exception):
        state = context_retention.empty_state()
        next_chunk, manifest, billable = 1, "plan-hash", 17
    async def model(*args, **kwargs):
        raise Continuation()
    monkeypatch.setattr(checkpoint, "ProgressContinuation", Continuation, raising=False)
    monkeypatch.setattr(checkpoint, "summarize_progress", model)
    result = await checkpoint_jobs.handle_checkpoint(make_job(state["messages"]))
    state["enabled"] = False
    await result.apply_domain(session)
    assert not state["inserts"] and not state["enqueued"]


async def test_real_split_plan_runs_each_utf8_segment_once_before_final_publish(environment, monkeypatch):
    import json
    from app.services import llm
    state, session = environment
    text = "가나다라"*1_000
    state["messages"] = [checkpoint.SourceMessage(1, "user", "normal", text)]
    consumed = []
    async def generate(system, convo, **kwargs):
        body = json.loads(convo[0]["content"])
        consumed.append(body["messages"][0]["content"])
        return llm.LLMResult(json.dumps({"base_checkpoint_id": body["base_checkpoint_id"],
            "source_message_ids": body["source_message_ids"], "updates": []}),
            10, 5, model="gpt-6-luna", finish_reason="stop")
    monkeypatch.setattr(checkpoint, "summarize_progress", _REAL_SUMMARIZE_PROGRESS)
    monkeypatch.setattr(llm, "generate", generate)
    job = make_job(state["messages"])
    segments = checkpoint.source_segments(state["messages"][0])
    for index in range(len(segments)):
        result = await checkpoint_jobs.handle_checkpoint(job)
        await result.apply_domain(session)
        if index + 1 < len(segments):
            assert not state["inserts"]
            job = replace(job, payload=state["enqueued"][-1]["payload"])
    assert "".join(consumed) == text and len(consumed) == len(segments)
    assert len(state["inserts"]) == 1 and state["inserts"][0]["state_json"]["sequence"] == 1


async def test_reverify_reads_more_than_four_hundred_messages_in_pages(environment):
    state, session = environment
    state["messages"] = [checkpoint.SourceMessage(i, "user", "normal", f"synthetic {i}")
                         for i in range(1, 402)]
    current = checkpoint.Checkpoint(uuid.uuid4(), 401, "", checkpoint.RETENTION_SUMMARIZER_VERSION,
        "a"*64, context_retention.empty_state(), {"processing_complete": True})
    state["current"] = current
    job = replace(make_job(state["messages"]), payload={"mode": "reverify",
        "memory_generation": 0, "checkpoint_id": str(current.id), "through_message_id": 401,
        "after_message_id": 0, "manifests": []})
    for page in range(4):
        result = await checkpoint_jobs.handle_checkpoint(job)
        await result.apply_domain(session)
        if page < 3:
            assert not state["inserts"]
            job = replace(job, payload=state["enqueued"][-1]["payload"])
    assert state["models"] == 4
    assert state["inserts"][0]["through_message_id"] == 401
    assert len(state["inserts"][0]["validation_json"]["page_manifests"]) == 4


async def test_reverify_continues_same_original_page_until_all_chunks_complete(environment, monkeypatch):
    state, session = environment
    current = checkpoint.Checkpoint(uuid.uuid4(), 1, "", checkpoint.RETENTION_SUMMARIZER_VERSION,
        "a"*64, context_retention.empty_state(), {"processing_complete": True})
    state["current"] = current
    class Continuation(Exception):
        state = context_retention.empty_state()
        next_chunk, manifest, billable = 1, "page-plan", 5
    async def model(*args, **kwargs):
        if kwargs["start_chunk"] == 0:
            raise Continuation()
        assert kwargs["continuation_manifest"] == "page-plan"
        return "", context_retention.empty_state(), SimpleNamespace(billable=3)
    monkeypatch.setattr(checkpoint, "ProgressContinuation", Continuation, raising=False)
    monkeypatch.setattr(checkpoint, "summarize_progress", model)
    job = replace(make_job(state["messages"]), payload={"mode": "reverify",
        "memory_generation": 0, "checkpoint_id": str(current.id), "through_message_id": 1,
        "after_message_id": 0, "manifests": []})
    first = await checkpoint_jobs.handle_checkpoint(job)
    await first.apply_domain(session)
    assert first.result_code == "reverify_progress_page" and not state["inserts"]
    continuation = state["enqueued"][-1]["payload"]
    assert continuation["after_message_id"] == 0 and continuation["manifests"] == []
    final = await checkpoint_jobs.handle_checkpoint(replace(job, payload=continuation))
    await final.apply_domain(session)
    assert final.result_code == "reverify_complete"
    assert len(state["inserts"][0]["validation_json"]["page_manifests"]) == 1


async def test_reverify_does_not_reactivate_historically_resolved_item(environment, monkeypatch):
    import json
    state, session = environment
    current = checkpoint.Checkpoint(uuid.uuid4(), 1, "", checkpoint.RETENTION_SUMMARIZER_VERSION,
        "a"*64, context_retention.empty_state(), {"processing_complete": True})
    state["current"] = current
    recovered = context_retention.apply_delta(json.dumps({"base_checkpoint_id": None,
        "source_message_ids": [1], "updates": [{"op": "add", "item_id": "old-topic",
        "kind": "topic", "text": "synthetic", "evidence": [{"message_id": 1, "quote": "hello"}]}]}),
        previous_state=None, previous_id=None, messages=state["messages"])
    async def model(*args, **kwargs):
        return "synthetic", recovered, SimpleNamespace(billable=1)
    async def history(*args, **kwargs):
        assert kwargs["item_id"] == "old-topic"
        return {"item": {**recovered["items"][0], "status": "resolved"}}
    monkeypatch.setattr(checkpoint, "summarize_progress", model)
    monkeypatch.setattr(context_retention, "load_historical_item", history)
    job = replace(make_job(state["messages"]), payload={"mode": "reverify",
        "memory_generation": 0, "checkpoint_id": str(current.id), "through_message_id": 1,
        "after_message_id": 0, "manifests": []})
    result = await checkpoint_jobs.handle_checkpoint(job)
    await result.apply_domain(session)
    assert state["inserts"][0]["state_json"]["items"] == []


async def test_defer_preserves_history_and_failure_budget():
    job = make_job([checkpoint.SourceMessage(1, "user", "normal", "hello")])
    class DeferredSession(Session):
        async def execute(self, statement, params):
            self.calls.append((str(statement), params))
            return SimpleNamespace(first=lambda: (job.id,))
    session = DeferredSession()
    assert await jobs.finalize_deferred(session, job, reason="dependency_pending")
    sql, values = session.calls[0]
    assert "max_attempts=max_attempts+1" in sql and "attempt=" not in sql
    assert "lease_token=:lease_token" in sql and "DELETE" not in sql
    assert len(session.calls) == 1 and session.commits == 1 and values["lease_token"] == job.lease_token


async def test_defer_with_lost_lease_does_not_touch_history():
    job = make_job([checkpoint.SourceMessage(1, "user", "normal", "hello")])
    class LostSession(Session):
        async def execute(self, statement, params):
            self.calls.append((str(statement), params))
            return SimpleNamespace(first=lambda: None)
    session = LostSession()
    assert not await jobs.finalize_deferred(session, job, reason="dependency_pending")
    assert len(session.calls) == 1 and session.rollbacks == 1 and not session.commits
