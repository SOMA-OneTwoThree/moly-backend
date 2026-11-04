"""Incremental checkpoint authority starts strictly after the legacy boundary."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import checkpoint, checkpoint_repo
from app.services.context_provenance import HistoryEntry, SourceRef, VerifiedCoverage, without_covered
from worker import checkpoint_jobs
from tests.test_retention_worker import environment as environment, make_job
from tests.test_checkpoint_packing_worker import packed as packed, next_job


UID = uuid.UUID(int=10)
BINDING = {"baseline_id": str(uuid.UUID(int=20)), "generation": 0,
           "privacy_epoch": 0, "source_after": 100,
           "legacy_checkpoint_id": None, "legacy_checkpoint_through": 0,
           "legacy_checkpoint_hash": None, "legacy_checkpoint_version": None,
           "legacy_contract_after": 100, "legacy_contract_id": None}


def source(identity):
    return checkpoint.SourceMessage(identity, "user", "normal", f"synthetic-{identity}")


def test_incremental_coverage_never_grants_legacy_prefix_omission():
    old = SourceRef(UID, 100, 0, "a" * 64)
    new = SourceRef(UID, 101, 0, "b" * 64)
    uncovered = SourceRef(UID, 104, 0, "c" * 64)
    coverage = VerifiedCoverage(UID, 0, uuid.UUID(int=30), 103, "d" * 64,
                                after_message_id=100).bind_sources([old, new, uncovered])
    entries = [HistoryEntry("user", "same", source_ref=ref) for ref in (old, new, uncovered)]
    assert without_covered(entries, coverage) == [entries[0], entries[2]]
    assert coverage.source_refs == frozenset([new])


def test_new_root_filters_only_incremental_source_without_promoting_old_summary():
    plan = checkpoint.plan([source(99), source(100), source(101), source(102)],
        previous=None, version=checkpoint.RETENTION_SUMMARIZER_VERSION,
        reason="input_pressure", through_message_id=102, baseline_binding=BINDING)
    assert plan.source_message_ids == (101, 102)
    assert plan.previous_id is None and plan.previous_through_message_id is None
    assert plan.payload()["baseline_binding"] == BINDING


def test_binding_is_hashed_but_unbound_checkpoint_hash_stays_compatible():
    rows = [source(101)]
    original = checkpoint.source_hash(previous=None, messages=rows,
                                      version=checkpoint.RETENTION_SUMMARIZER_VERSION)
    assert original == checkpoint.source_hash(previous=None, messages=rows,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION, baseline_binding=None)
    bound = checkpoint.source_hash(previous=None, messages=rows,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION, baseline_binding=BINDING)
    assert original != bound
    assert bound != checkpoint.source_hash(previous=None, messages=rows,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION,
        baseline_binding={**BINDING, "baseline_id": str(uuid.UUID(int=21))})


async def test_bound_root_rejects_an_omitted_source_between_baseline_and_first_payload(monkeypatch):
    req = checkpoint_jobs._Request(UID, 103, (102, 103), "a" * 64,
        checkpoint.RETENTION_SUMMARIZER_VERSION, None, None, 0,
        baseline_binding=BINDING)
    loader = AsyncMock(return_value=[source(101), source(102), source(103)])
    monkeypatch.setattr(checkpoint_repo, "load_range", loader)
    assert await checkpoint_jobs._load_sources(object(), req, None) == []
    assert loader.await_args.kwargs["after_id"] == 100


async def test_pressure_starts_after_baseline_not_at_zero(monkeypatch):
    monkeypatch.setattr(checkpoint_repo, "load_latest", AsyncMock(return_value=None))
    monkeypatch.setattr(checkpoint_repo, "load_compat_baseline", AsyncMock(return_value=SimpleNamespace(
        source_after=100, payload=lambda: dict(BINDING))))
    from app.services import context_retention
    monkeypatch.setattr(context_retention, "enabled_now", AsyncMock(return_value=True))
    monkeypatch.setattr(context_retention, "adopted_user", AsyncMock(return_value=True))
    monkeypatch.setattr(checkpoint_repo, "ensure_pressure_ingest", AsyncMock())
    monkeypatch.setattr(checkpoint_repo, "wake_checkpoint_ingest", AsyncMock())
    monkeypatch.setattr(checkpoint_repo.retention_release, "scalar", AsyncMock(side_effect=[102, 102]))
    monkeypatch.setattr(checkpoint_repo.retention_release, "execute", AsyncMock(
        return_value=SimpleNamespace(first=lambda: None)))
    loader = AsyncMock(return_value=[source(101), source(102)])
    monkeypatch.setattr(checkpoint_repo, "load_pressure_sources", loader)
    monkeypatch.setattr(checkpoint_repo, "maybe_enqueue", AsyncMock(return_value=uuid.UUID(int=40)))
    result = await checkpoint_repo.request_pressure(object(), user_id=UID, generation=0)
    assert result.status == "queued"
    assert loader.await_args.kwargs["after_id"] == 100


@pytest.fixture
def compat_environment(environment, monkeypatch):
    state, session = environment
    state["binding"] = dict(BINDING)

    async def baseline(*args, **kwargs):
        value = dict(state["binding"])
        return SimpleNamespace(source_after=value["source_after"], payload=lambda: value)

    monkeypatch.setattr(checkpoint_repo, "load_compat_baseline", baseline)
    state["messages"] = [source(99), source(100), source(101), source(102)]
    return state, session


def bound_job(messages):
    job = make_job(messages)
    return replace(job, payload={**job.payload, "baseline_binding": dict(BINDING),
        "source_hash": checkpoint.source_hash(previous=None, messages=messages,
            version=checkpoint.RETENTION_SUMMARIZER_VERSION, baseline_binding=BINDING)})


async def test_complete_draft_and_publication_preserve_binding_without_legacy_source(compat_environment):
    state, session = compat_environment
    job = bound_job(state["messages"][2:])
    result = await checkpoint_jobs.handle_checkpoint(job)
    await result.apply_domain(session)
    child = replace(job, payload=state["enqueued"][-1]["payload"])
    assert child.payload["baseline_binding"] == BINDING
    assert child.payload["source_message_ids"] == [101, 102]
    assert state["models"] == 1
    result = await checkpoint_jobs.handle_checkpoint(child)
    await result.apply_domain(session)
    assert state["models"] == 1
    assert state["inserts"][0]["validation_json"]["baseline_binding"] == BINDING
    assert state["inserts"][0]["coverage_from"] == 101


@pytest.mark.parametrize("phase", ["before_model", "after_model", "after_draft"])
async def test_baseline_change_cannot_rebind_old_work(compat_environment, phase):
    state, session = compat_environment
    job = bound_job(state["messages"][2:])
    if phase == "before_model":
        state["binding"]["baseline_id"] = str(uuid.UUID(int=21))
        result = await checkpoint_jobs.handle_checkpoint(job)
        assert result.result_code == checkpoint_jobs.RESULT_SOURCE_CHANGED
        assert state["models"] == 0
    else:
        result = await checkpoint_jobs.handle_checkpoint(job)
        if phase == "after_draft":
            await result.apply_domain(session)
            job = replace(job, payload=state["enqueued"][-1]["payload"])
        state["binding"]["baseline_id"] = str(uuid.UUID(int=21))
        if phase == "after_draft":
            result = await checkpoint_jobs.handle_checkpoint(job)
            assert result.result_code == checkpoint_jobs.RESULT_SOURCE_CHANGED
        else:
            await result.apply_domain(session)
            assert not state["enqueued"]
    assert not state["inserts"]


async def test_reverify_reads_only_new_interval_and_preserves_binding(compat_environment):
    from app.services import context_retention
    state, session = compat_environment
    current = checkpoint.Checkpoint(uuid.UUID(int=50), 102, "new state only",
        checkpoint.RETENTION_SUMMARIZER_VERSION, "e" * 64, context_retention.empty_state(),
        {"baseline_binding": dict(BINDING)})
    state["current"] = current
    job = replace(make_job(state["messages"][2:]), payload={
        "mode": "reverify", "schema_version": checkpoint.SCHEMA_VERSION,
        "summarizer_version": checkpoint.RETENTION_SUMMARIZER_VERSION,
        "checkpoint_id": str(current.id), "through_message_id": 102,
        "after_message_id": 100, "memory_generation": 0, "privacy_epoch": 0,
        "manifests": [], "baseline_binding": dict(BINDING)})
    result = await checkpoint_jobs.handle_checkpoint(job)
    await result.apply_domain(session)
    validation = state["inserts"][0]["validation_json"]
    assert validation["baseline_binding"] == BINDING
    assert validation["page_manifests"][0]["after"] == 100
    assert state["inserts"][0]["coverage_from"] == 101


def test_completed_binding_cannot_swap_baseline_identity():
    job = bound_job([source(101)])
    req = checkpoint_jobs._parse(job)
    first = checkpoint_jobs._completed_binding(req, job.payload)
    changed = replace(req, baseline_binding={**BINDING, "baseline_id": str(uuid.UUID(int=99))})
    assert checkpoint_jobs._completed_binding(changed, job.payload) != first


def test_final_insert_and_draft_cas_include_current_metadata():
    assert "cc.retention_compat_json" in str(checkpoint_repo._INSERT_RETENTION_SQL)
    assert "cc.retention_compat_json" in str(checkpoint_jobs._STORE_COMPLETED_PARENT)
    assert "memory_generation=:expected_generation" in str(checkpoint_repo._INSERT_RETENTION_SQL)


@pytest.mark.parametrize("field,value", [("source_after", -1), ("source_after", True),
                                        ("source_after", "100"), ("generation", True),
                                        ("generation", 1), ("privacy_epoch", 1)])
def test_malformed_or_rebound_baseline_is_rejected_before_model(field, value):
    job = bound_job([source(101)])
    job = replace(job, payload={**job.payload, "baseline_binding": {**BINDING, field: value}})
    with pytest.raises(checkpoint_jobs.JobFatal, match="invalid_baseline_binding"):
        checkpoint_jobs._parse(job)


def test_incremental_evidence_cannot_reference_opaque_legacy_summary_source():
    from app.services import context_retention
    state = {**context_retention.empty_state(), "sequence": 1, "items": [{
        "id": "topic-1", "kind": "topic", "text": "synthetic", "status": "active",
        "evidence": [{"message_id": 100, "start_utf8": 0, "end_utf8": 1, "source_hash": "a"*64}],
        "changed_sequence": 1, "idle_checkpoints": 0}]}
    with pytest.raises(context_retention.RetentionError, match="compat_baseline_evidence"):
        checkpoint_repo.validate_compat_state(state, BINDING)


async def test_old_parent_is_not_used_as_a_new_structured_root():
    previous = checkpoint.Checkpoint(uuid.UUID(int=90), 100, "opaque old summary",
                                    checkpoint.LEGACY_SUMMARIZER_VERSION, "a"*64)
    with pytest.raises(checkpoint.CheckpointError, match="compat_baseline_parent"):
        checkpoint.plan([source(101)], previous=previous,
            version=checkpoint.RETENTION_SUMMARIZER_VERSION, reason="input_pressure",
            through_message_id=101, baseline_binding=BINDING)


async def test_packed_partial_pages_keep_binding_and_never_process_legacy_prefix(packed, monkeypatch):
    import copy
    state, session, original = packed
    state["messages"] = [replace(state["messages"][0], id=101)]
    payload = {**original.payload, "baseline_binding": dict(BINDING),
        "source_message_ids": [101], "through_message_id": 101, "packing_chunk_cap": 1,
        "source_hash": checkpoint.source_hash(previous=None, messages=state["messages"],
            version=checkpoint.RETENTION_SUMMARIZER_VERSION, baseline_binding=BINDING)}
    job = replace(original, payload=payload)
    state["durable"] = copy.deepcopy(payload)
    monkeypatch.setattr(checkpoint_repo, "load_compat_baseline", AsyncMock(return_value=SimpleNamespace(
        source_after=100, payload=lambda: dict(BINDING))))
    for _ in range(20):
        result = await checkpoint_jobs.handle_checkpoint(job)
        await result.apply_domain(session)
        child = next_job(job, state)
        assert child.payload["baseline_binding"] == BINDING
        assert child.payload["source_message_ids"] == [101]
        if child.payload.get("mode") == "publish_retention":
            result = await checkpoint_jobs.handle_checkpoint(child)
            before_publish = len(state["provider"])
            await result.apply_domain(session)
            assert len(state["provider"]) == before_publish
            break
        job = child
    else:
        pytest.fail("bounded partial chain did not complete")
    assert len(state["provider"]) > 1
    assert all(item["source_message_ids"] == [101] for item in state["provider"])
    assert state["inserts"][0]["validation_json"]["baseline_binding"] == BINDING


async def test_ready_plan_ignores_legacy_rows_and_carries_the_baseline(monkeypatch):
    class Session:
        async def execute(self, *args):
            return SimpleNamespace(first=lambda: None)
    monkeypatch.setattr(checkpoint_repo, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(checkpoint_repo, "load_latest", AsyncMock(return_value=None))
    monkeypatch.setattr(checkpoint_repo, "read_memory_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(checkpoint_repo, "load_compat_baseline", AsyncMock(return_value=SimpleNamespace(
        payload=lambda: dict(BINDING))))
    enqueue = AsyncMock(return_value=uuid.UUID(int=70))
    monkeypatch.setattr(checkpoint_repo, "enqueue_checkpoint", enqueue)
    await checkpoint_repo._maybe_enqueue_retention(Session(), user_id=UID,
        messages=[source(99), source(100), source(101), source(102)],
        keep_from_message_id=None, reset_triggered=False, pressure_through_message_id=102)
    plan = enqueue.await_args.kwargs["plan"]
    assert plan.source_message_ids == (101, 102)
    assert plan.baseline_binding == BINDING and plan.previous_id is None


@pytest.mark.parametrize("pressure", [None, 102])
async def test_sweep_does_not_enroll_an_unrequested_legacy_user(monkeypatch, pressure):
    from tests.test_checkpoint_sweep_resume import install
    from worker import memory_sweep_jobs as sweep
    env = install(monkeypatch, pending=None, pressure=pressure,
        messages=[source(99), source(100), source(101), source(102)])
    monkeypatch.setattr(checkpoint_repo, "load_latest", AsyncMock(return_value=None))
    await sweep._resume_retention_checkpoints()
    assert not any(kind in {"page", "enqueue", "resume_enqueue", "dead", "unpublished"}
                   for kind, _ in env.calls)


@pytest.mark.parametrize("pressure", [None, 104])
async def test_sweep_new_suffix_starts_after_baseline(monkeypatch, pressure):
    from tests.test_checkpoint_sweep_resume import install, message
    from worker import memory_sweep_jobs as sweep
    env = install(monkeypatch, pending=103, pressure=pressure, messages=[message(99), message(100),
        message(101), message(102, "moly"), message(103), message(104, "moly")])
    monkeypatch.setattr(checkpoint_repo, "load_latest", AsyncMock(return_value=None))
    binding = {**BINDING, "generation": 7}
    monkeypatch.setattr(checkpoint_repo, "load_compat_baseline", AsyncMock(return_value=SimpleNamespace(
        source_after=100, payload=lambda: binding)))
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 1
    pages = [params for kind, params in env.calls if kind == "page"]
    assert pages[0]["after_id"] == 100
    queued = next(params for kind, params in env.calls if kind == "enqueue")
    assert all(item.id > 100 for item in queued["messages"])


def test_sweep_candidate_sql_excludes_uninitialized_legacy_users_but_preserves_namespace():
    from worker import memory_sweep_jobs as sweep
    sql = str(sweep._RETENTION_RESUME_USERS)
    assert "CAST(:allow_unbound AS boolean) OR cc.retention_compat_json IS NOT NULL" in sql
    assert "OR latest.through_message_id IS NOT NULL" in sql
    assert "cc.retention_compat_json->>'source_after'" in sql
    assert "CASE WHEN CAST(:allow_unbound AS boolean) THEN 0" in sql


async def test_fresh_producer_without_baseline_or_chain_never_queues_history(monkeypatch):
    monkeypatch.setattr(checkpoint_repo, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(checkpoint_repo, "load_latest", AsyncMock(return_value=None))
    monkeypatch.setattr(checkpoint_repo, "read_memory_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(checkpoint_repo, "load_compat_baseline", AsyncMock(return_value=None))
    enqueue = AsyncMock()
    monkeypatch.setattr(checkpoint_repo, "enqueue_checkpoint", enqueue)
    result = await checkpoint_repo._maybe_enqueue_retention(object(), user_id=UID,
        messages=[source(1), source(2)], keep_from_message_id=None,
        reset_triggered=False, pressure_through_message_id=2)
    assert result is None
    enqueue.assert_not_awaited()


async def test_pressure_without_baseline_or_chain_never_writes_target(monkeypatch):
    from app.services import context_retention
    monkeypatch.setattr(context_retention, "enabled_now", AsyncMock(return_value=True))
    monkeypatch.setattr(context_retention, "adopted_user", AsyncMock(return_value=True))
    monkeypatch.setattr(checkpoint_repo, "load_latest", AsyncMock(return_value=None))
    monkeypatch.setattr(checkpoint_repo, "load_compat_baseline", AsyncMock(return_value=None))
    scalar = AsyncMock(return_value=2)
    monkeypatch.setattr(checkpoint_repo.retention_release, "scalar", scalar)
    loader = AsyncMock()
    monkeypatch.setattr(checkpoint_repo, "load_pressure_sources", loader)
    result = await checkpoint_repo.request_pressure(object(), user_id=UID, generation=0)
    assert result.status == "not_initialized"
    assert scalar.await_count == 1  # Only committed-boundary read; no target write.
    loader.assert_not_awaited()


@pytest.mark.parametrize("private_preparation", [False, True])
async def test_unbound_existing_chain_and_private_preparation_keep_fresh_producers(
    monkeypatch, private_preparation,
):
    from contextlib import nullcontext
    from app.services.retention_preparation_context import Binding, use_binding
    from app.services import context_retention
    class Session:
        async def execute(self, *args):
            return SimpleNamespace(first=lambda: None)
    previous = None if private_preparation else checkpoint.Checkpoint(uuid.UUID(int=80), 100, "old v5",
        checkpoint.RETENTION_SUMMARIZER_VERSION, "a"*64, context_retention.empty_state(), {})
    monkeypatch.setattr(checkpoint_repo, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(checkpoint_repo, "load_latest", AsyncMock(return_value=previous))
    monkeypatch.setattr(checkpoint_repo, "read_memory_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(checkpoint_repo, "load_compat_baseline", AsyncMock(return_value=None))
    enqueue = AsyncMock(return_value=uuid.UUID(int=81))
    monkeypatch.setattr(checkpoint_repo, "enqueue_checkpoint", enqueue)
    scope = use_binding(Binding(uuid.UUID(int=82), True)) if private_preparation else nullcontext()
    with scope:
        result = await checkpoint_repo._maybe_enqueue_retention(Session(), user_id=UID,
            messages=[source(101), source(102)], keep_from_message_id=None,
            reset_triggered=False, pressure_through_message_id=102)
    assert result is not None
    assert enqueue.await_args.kwargs["plan"].baseline_binding is None
    assert enqueue.await_args.kwargs["plan"].previous_id == (previous.id if previous else None)
