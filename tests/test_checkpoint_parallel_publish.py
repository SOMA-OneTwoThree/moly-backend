"""Unpublished pages overlap dependencies; publication is fenced and model-free."""
import copy
import hashlib
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import checkpoint as cp, checkpoint_repo as repo, context_retention as cr
from worker import checkpoint_jobs as worker
from worker.consumer import JobCancelled, JobDeferred, JobFatal

_REAL_STORE_COMPLETED_PARENT = worker._store_completed_parent


@pytest.fixture
def env(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def commit(self):
            pass

    uid = uuid.uuid4()
    sources = [cp.SourceMessage(1, "user", "normal", "Synthetic source " * 180),
               cp.SourceMessage(2, "moly", "normal", "Synthetic response")]
    plan = cp.plan(sources, previous=None, memory_generation=2,
        version=cp.RETENTION_SUMMARIZER_VERSION, reason="input_pressure", through_message_id=2)
    payload = replace(plan, privacy_epoch=3).payload()
    state = {**cr.empty_state(), "sequence": 1}
    enabled, privacy, complete = AsyncMock(return_value=True), AsyncMock(return_value=True), AsyncMock(return_value=True)
    enqueue, inserted = AsyncMock(return_value=uuid.uuid4()), AsyncMock(return_value=uuid.uuid4())
    archived = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(worker.config_store, "get_config_values", AsyncMock(return_value={"context_retention_v2_enabled": True}))
    monkeypatch.setattr(cr, "enabled_now", enabled)
    monkeypatch.setattr(cr, "adopted_user", AsyncMock(return_value=True))
    monkeypatch.setattr(cr, "privacy_epoch_matches", privacy, raising=False)
    monkeypatch.setattr(cr, "processing_complete", complete)
    monkeypatch.setattr(cr, "validate_progress_sources", AsyncMock())
    monkeypatch.setattr(cr, "enqueue_contract_processing", AsyncMock())
    monkeypatch.setattr(worker, "_wake_ingest_dependency", AsyncMock())
    monkeypatch.setattr(worker, "_still_applicable", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(worker, "_load_sources", AsyncMock(return_value=sources))
    monkeypatch.setattr(repo, "load_user_state", AsyncMock(return_value=(None, "ko")))
    monkeypatch.setattr(worker, "_generation_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(repo, "count", AsyncMock(return_value=0))
    monkeypatch.setattr(repo, "insert", inserted)
    monkeypatch.setattr(repo, "clear_completed_pressure", AsyncMock())
    monkeypatch.setattr(repo, "reserve_remaining_pressure", AsyncMock(), raising=False)
    monkeypatch.setattr(worker.jobs, "enqueue", enqueue)
    monkeypatch.setattr(worker, "_store_completed_parent", archived)
    model = AsyncMock(return_value=("", state, SimpleNamespace(billable=0)))
    monkeypatch.setattr(cp, "summarize_progress", model)
    job = SimpleNamespace(id=uuid.uuid4(), user_id=uid, payload=payload, attempt=1, max_attempts=3)
    return SimpleNamespace(job=job, session=Session(), state=state, sources=sources,
        complete=complete, enqueue=enqueue, inserted=inserted, model=model, privacy=privacy,
        archived=archived, enabled=enabled)


async def ready_draft(env):
    result = await worker.handle_checkpoint(env.job)
    assert result.result_code == "checkpoint_draft_ready"
    await result.apply_domain(env.session)
    env.inserted.assert_not_awaited()
    payload = env.enqueue.await_args.kwargs["payload"]
    assert payload["mode"] == "publish_retention" and payload["privacy_epoch"] == 3
    env.enqueue.reset_mock()
    return SimpleNamespace(**{**vars(env.job), "id": uuid.uuid4(), "payload": payload})


async def test_pages_calculate_while_dependencies_pending_and_last_page_is_always_durable(env):
    env.complete.return_value = False
    job = await ready_draft(env)
    env.model.assert_awaited_once()
    cr.enqueue_contract_processing.assert_awaited_once()
    worker._wake_ingest_dependency.assert_awaited_once()
    assert "completed_proof" in job.payload


async def test_partial_page_persists_without_processing_complete(env):
    env.complete.return_value = False
    env.model.side_effect = cp.ProgressContinuation(state=cr.empty_state(), next_chunk=1,
        manifest=cp._progress_manifest(cp.progress_batches(env.sources, previous=None, language="ko"),
                                      previous=None, language="ko"), billable=1)
    result = await worker.handle_checkpoint(env.job)
    await result.apply_domain(env.session)
    assert result.result_code == "checkpoint_progress_page"
    assert env.enqueue.await_args.kwargs["payload"]["progress_next_chunk"] == 1
    env.inserted.assert_not_awaited()


async def test_publish_wait_never_calls_model_and_preserves_exact_draft(env):
    job = await ready_draft(env)
    env.model.reset_mock()
    env.complete.return_value = False
    with pytest.raises(JobDeferred, match="checkpoint_source_processing_pending"):
        await worker.handle_checkpoint(job)
    env.model.assert_not_awaited()
    env.inserted.assert_not_awaited()
    env.enqueue.assert_not_awaited()  # JobDeferred retains the same claimed job.


@pytest.mark.parametrize("attempt,delay", [(1, 5.0), (120, 5.0), (121, 60.0), (170, 60.0),
                                           (171, 300.0), (10_000, 300.0)])
async def test_publish_wait_widens_its_interval_by_claim_count_and_never_gives_up(env, attempt, delay):
    job = await ready_draft(env)
    job.attempt, job.max_attempts = attempt, attempt + 2  # 대기는 매번 max_attempts를 같이 늘린다.
    env.model.reset_mock()
    env.complete.return_value = False
    with pytest.raises(JobDeferred, match="checkpoint_source_processing_pending") as caught:
        await worker.handle_checkpoint(job)
    assert caught.value.delay_s == delay
    env.model.assert_not_awaited()


async def test_final_apply_regression_durably_requeues_same_draft_without_resetting_failure_budget(env):
    job = await ready_draft(env)
    job.attempt, job.max_attempts = 5, 5  # two deferrals and two failures; one remaining.
    env.model.reset_mock()
    env.complete.side_effect = [True, False]
    result = await worker.handle_checkpoint(job)
    await result.apply_domain(env.session)
    kwargs = env.enqueue.await_args.kwargs
    assert kwargs["payload"] == job.payload
    assert kwargs["max_attempts"] == 1  # current successful wait does not spend an attempt.
    assert kwargs["dedup_key"] == f"checkpoint-publish-wait:{job.id}"
    env.model.assert_not_awaited()
    env.inserted.assert_not_awaited()


async def test_publish_success_is_model_free_and_resumes_only_after_actual_insert(env):
    job = await ready_draft(env)
    env.model.reset_mock()
    result = await worker.handle_checkpoint(job)
    await result.apply_domain(env.session)
    env.model.assert_not_awaited()
    assert env.inserted.await_args.kwargs["validation_json"]["processing_complete"] is True
    repo.reserve_remaining_pressure.assert_awaited_once()
    assert repo.reserve_remaining_pressure.await_args.kwargs["privacy_epoch"] == 3


@pytest.mark.parametrize("change", ["partial", "manifest", "chunks", "state", "binding", "bool_chunks"])
async def test_incomplete_or_tampered_draft_is_rejected_without_model(env, change):
    job = await ready_draft(env)
    job.payload = copy.deepcopy(job.payload)
    if change == "partial":
        job.payload["completed_state"]["sequence"] = 0
    elif change == "manifest":
        job.payload["completed_proof"]["manifest"] = "wrong"
    elif change == "chunks":
        job.payload["completed_proof"]["completed_chunks"] = 0
    elif change == "bool_chunks":
        job.payload["completed_proof"]["completed_chunks"] = True
    elif change == "state":
        job.payload["completed_proof"]["state_hash"] = "wrong"
    else:
        job.payload["source_message_ids"] = [1]
    if change != "binding":
        job.payload["completed_binding"] = worker._completed_binding(worker._parse(job), job.payload)
    env.model.reset_mock()
    with pytest.raises(JobFatal, match="checkpoint_completed_proof_invalid"):
        await worker.handle_checkpoint(job)
    env.model.assert_not_awaited()


async def test_original_privacy_epoch_is_checked_before_work_and_again_before_persist(env):
    env.privacy.side_effect = [True, False, False]
    result = await worker.handle_checkpoint(env.job)
    await result.apply_domain(env.session)
    env.enqueue.assert_not_awaited()
    env.inserted.assert_not_awaited()
    assert env.privacy.await_args_list[-2].kwargs == {"user_id": env.job.user_id, "privacy_epoch": 3, "lock": True}


async def test_old_epoch_is_not_rebound_for_completed_draft(env):
    job = await ready_draft(env)
    env.privacy.return_value = False
    env.model.reset_mock()
    with pytest.raises(JobCancelled, match="checkpoint_privacy_changed"):
        await worker.handle_checkpoint(job)
    env.model.assert_not_awaited()


async def test_publish_privacy_lock_contention_rolls_back_same_draft_without_new_artifact(env):
    job = await ready_draft(env)
    env.privacy.side_effect = [True, False, True]
    env.model.reset_mock()
    result = await worker.handle_checkpoint(job)
    with pytest.raises(worker._PrivacyLockBusy, match="checkpoint_privacy_busy"):
        await result.apply_domain(env.session)
    env.enqueue.assert_not_awaited()
    env.inserted.assert_not_awaited()
    env.model.assert_not_awaited()
    assert job.payload["mode"] == "publish_retention"


async def test_one_page_off_during_model_keeps_completed_parent_payload_then_on_publishes_without_model(env):
    result = await worker.handle_checkpoint(env.job)
    env.enabled.return_value = False
    await result.apply_domain(env.session)
    env.enqueue.assert_not_awaited()
    env.inserted.assert_not_awaited()
    archived_payload = env.archived.await_args.args[2]
    assert archived_payload["mode"] == "publish_retention"
    assert archived_payload["completed_state"]["sequence"] == 1
    env.enabled.return_value = True
    env.model.reset_mock()
    resumed = SimpleNamespace(**{**vars(env.job), "payload": archived_payload})
    publish = await worker.handle_checkpoint(resumed)
    await publish.apply_domain(env.session)
    env.model.assert_not_awaited()
    env.inserted.assert_awaited_once()


async def test_periodic_reverify_precedes_remaining_pressure_without_two_active_plans(env, monkeypatch):
    monkeypatch.setattr(worker.settings, "context_checkpoint_reverify_every", 1)
    job = await ready_draft(env)
    result = await worker.handle_checkpoint(job)
    await result.apply_domain(env.session)
    assert env.enqueue.await_args.kwargs["payload"]["mode"] == "reverify"
    repo.reserve_remaining_pressure.assert_not_awaited()


async def test_parent_rewrite_preserves_expiry_redaction_and_uses_canonical_payload_hash(env):
    job = await ready_draft(env)
    captured = []
    class Session:
        async def execute(self, statement, params):
            captured.append((str(statement), params))
            return SimpleNamespace(first=lambda: (job.id,))
    assert await _REAL_STORE_COMPLETED_PARENT(Session(), env.job, job.payload)
    sql, params = captured[0]
    assert json.loads(params["payload"]) == job.payload
    assert hashlib.sha256(params["payload"].encode()).hexdigest() == params["payload_hash"]
    assert "state='succeeded'" in sql and "payload_redacted_at IS NULL" in sql
    assert "payload_expires_at IS NULL OR payload_expires_at>now()" in sql
    assignments = sql.split(" SET ", 1)[1].split("WHERE", 1)[0]
    assert "payload_expires_at" not in assignments and "payload_redacted_at" not in assignments
    assert "result_detail" not in assignments and "max_attempts" not in assignments


@pytest.mark.parametrize("language,text", [("ko", "가나다🙂"), ("en", "synthetic🙂"), ("ja", "ひらがな🙂")])
def test_completed_proof_reconstructs_all_unicode_fragments_and_parent_sequence(language, text):
    parent = cp.Checkpoint(uuid.uuid4(), 9, "", cp.RETENTION_SUMMARIZER_VERSION,
        "a" * 64, {**cr.empty_state(), "sequence": 7})
    sources = [cp.SourceMessage(10, "user", "normal", text * 900)]
    final = {**cr.empty_state(), "sequence": 8}
    proof = cp.completed_progress_proof(messages=sources, previous=parent, language=language, state=final)
    assert proof["completed_chunks"] == proof["total_chunks"] > 1
    assert cp.validate_completed_progress(messages=sources, previous=parent, language=language,
        state=final, proof=proof) == ""
    for corrupt in ({**proof, "completed_chunks": proof["total_chunks"] - 1},
                    {**proof, "manifest": "changed"}, {**proof, "state_hash": "changed"}):
        with pytest.raises(cp.CheckpointError, match="progress_completed_proof"):
            cp.validate_completed_progress(messages=sources, previous=parent, language=language,
                state=final, proof=corrupt)
    with pytest.raises(cp.CheckpointError, match="progress_completed_sequence"):
        cp.completed_progress_proof(messages=sources, previous=parent, language=language,
            state={**final, "sequence": 7})


async def test_invalid_intermediate_manifest_never_persists_a_progress_child(env):
    env.model.side_effect = cp.ProgressContinuation(state=cr.empty_state(), next_chunk=1,
                                                   manifest="wrong", billable=1)
    result = await worker.handle_checkpoint(env.job)
    with pytest.raises(cp.CheckpointError, match="progress_page_proof"):
        await result.apply_domain(env.session)
    env.enqueue.assert_not_awaited()
