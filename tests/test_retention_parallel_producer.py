"""선제 고정 snapshot·privacy epoch·게시 후 후속예약의 오프라인 검증."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import checkpoint, checkpoint_repo as repo, context_retention as retention, jobs, privacy
from tests.test_checkpoint_parallel_publish import env as env


class Result:
    def __init__(self, row=None):
        self.row = row
    def first(self):
        return self.row


@pytest.mark.parametrize("mode,barrier,epoch,expected", [
    ("compat", ("active", 3, None), 3, True),
    ("compat", ("active", 4, None), 3, False),
    ("compat", ("deleting", 3, None), 3, False),
    ("compat", None, 0, True), ("enforced", None, 0, False),
    ("compat", None, 3, False), ("compat", ("active", 0, None), None, False),
])
async def test_privacy_epoch_never_rebinds_old_work(monkeypatch, mode, barrier, epoch, expected):
    monkeypatch.setattr(retention.settings, "privacy_barrier_mode", mode)
    current = privacy.BarrierState(*(barrier or ("missing", 0, None)))
    monkeypatch.setattr(privacy, "load_barrier", AsyncMock(return_value=current))
    assert await retention.privacy_epoch_matches(None, user_id=uuid.uuid4(), privacy_epoch=epoch) is expected


@pytest.mark.parametrize("exists", [True, False])
async def test_locked_existing_barrier_is_not_compat_missing(monkeypatch, exists):
    monkeypatch.setattr(retention.settings, "privacy_barrier_mode", "compat")
    class Session:
        async def execute(self, sql, params):
            assert sql is privacy._LOAD_BARRIER_NONBLOCKING
            return Result()
        async def scalar(self, sql, params):
            assert sql is privacy._HAS_BARRIER
            return exists
    assert await retention.privacy_epoch_matches(Session(), user_id=uuid.uuid4(), privacy_epoch=0, lock=True) is not exists


@pytest.mark.parametrize("made", [True, False])
async def test_new_fixed_snapshot_captures_epoch_and_reserves_exact_dependencies_only_when_created(monkeypatch, made):
    uid, identity, calls = uuid.uuid4(), uuid.uuid4(), []
    plan = checkpoint.CheckpointPlan(12, (11, 12), "a"*64, None, None,
                                    version=checkpoint.RETENTION_SUMMARIZER_VERSION, memory_generation=2)
    monkeypatch.setattr(retention, "capture_privacy_epoch", AsyncMock(return_value=4))
    monkeypatch.setattr(retention, "privacy_epoch_matches", AsyncMock(return_value=True))
    async def enqueue(session, **kwargs):
        calls.append(kwargs)
        return identity if made else None
    dependencies = AsyncMock()
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(repo, "reserve_checkpoint_dependencies", dependencies)
    assert await repo.enqueue_checkpoint(None, user_id=uid, plan=plan) == (identity if made else None)
    assert calls[0]["payload"]["privacy_epoch"] == 4
    assert calls[0]["payload"]["through_message_id"] == 12
    assert dependencies.await_count == int(made)
    if made:
        reserved = dependencies.await_args.kwargs["plan"]
        assert reserved.privacy_epoch == 4 and reserved.through_message_id == 12
    assert plan.privacy_epoch is None  # No mutable plan/payload from an existing job.


async def test_dependencies_share_fixed_through_and_epoch_without_summary_as_fact(monkeypatch):
    uid = uuid.uuid4()
    plan = checkpoint.CheckpointPlan(12, (11, 12), "a"*64, None, None,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION, memory_generation=2, privacy_epoch=4)
    monkeypatch.setattr(retention, "enabled_now", AsyncMock(return_value=True))
    monkeypatch.setattr(retention, "adopted_user", AsyncMock(return_value=True))
    monkeypatch.setattr(retention, "privacy_epoch_matches", AsyncMock(return_value=True))
    contract, ingest = AsyncMock(), AsyncMock()
    monkeypatch.setattr(retention, "enqueue_contract_processing", contract)
    monkeypatch.setattr(repo, "wake_checkpoint_ingest", ingest)
    await repo.reserve_checkpoint_dependencies(None, user_id=uid, plan=plan)
    assert contract.await_args.kwargs == {"user_id": uid, "generation": 2, "through_id": 12, "privacy_epoch": 4}
    assert ingest.await_args.kwargs == {"user_id": uid, "generation": 2, "through_id": 12}


@pytest.fixture
def followup(monkeypatch):
    uid = uuid.uuid4()
    state = {"goal": 20, "generation": 2, "published": 12, "publication_rows": 1,
             "user_busy": False, "cc_busy": False, "enabled": True, "adopted": True,
             "epoch_ok": True, "error": False, "queued": [], "settings": ("0", "0")}
    events = []
    class Savepoint:
        async def __aenter__(self):
            self.before = (state["goal"], list(state["queued"]), state["settings"])
            events.append("savepoint")
        async def __aexit__(self, error_type, error, tb):
            if error_type:
                state["goal"], state["queued"], state["settings"] = self.before
                events.append("rollback_savepoint")
    class Session:
        def begin_nested(self):
            return Savepoint()
        async def execute(self, sql, params=None):
            if sql is repo._FOLLOWUP_TIMEOUTS:
                return Result(state["settings"])
            if sql is repo._FOLLOWUP_SET_TIMEOUTS:
                state["settings"] = (params["lock_timeout"], params["statement_timeout"])
                return Result()
            assert sql is repo._REMAINING_PRESSURE_GOAL
            events.append("cc_skip_locked")
            eligible = (not state["cc_busy"] and state["generation"] == params["generation"]
                and state["goal"] is not None and state["goal"] > params["published_through"])
            return Result((state["goal"],) if eligible else None)
        async def scalar(self, sql, params):
            assert sql is repo._TRY_PRESSURE_USER
            events.append("try_user")
            return not state["user_busy"]
        async def commit(self):
            raise AssertionError("follow-up must share caller commit")
    async def enabled(*args, **kwargs):
        return state["enabled"]
    async def adopted(*args, **kwargs):
        return state["adopted"]
    async def epoch(*args, **kwargs):
        assert kwargs == {"user_id": uid, "privacy_epoch": 4, "lock": True}
        return state["epoch_ok"]
    async def reserve(session, **kwargs):
        assert kwargs == {"user_id": uid, "generation": 2, "through_id": 20}
        state["queued"].append(kwargs)
        events.append("reserve")
        if state["error"]:
            state["goal"] = 99
            raise RuntimeError("synthetic-private-error")
        return repo.PressureReservation("queued", 20, uuid.uuid4())
    monkeypatch.setattr(retention, "enabled_now", enabled)
    monkeypatch.setattr(retention, "adopted_user", adopted)
    monkeypatch.setattr(retention, "privacy_epoch_matches", epoch)
    monkeypatch.setattr(repo, "request_pressure", reserve)
    return SimpleNamespace(session=Session(), uid=uid, state=state, events=events)


async def test_publication_followup_only_reserves_original_remaining_goal_once(followup):
    e = followup
    assert await repo.reserve_remaining_pressure(e.session, user_id=e.uid, generation=2,
                                                published_through_id=12, privacy_epoch=4)
    assert len(e.state["queued"]) == 1 and e.state["goal"] == 20
    assert e.events == ["savepoint", "try_user", "cc_skip_locked", "reserve"]
    assert e.state["settings"] == ("0", "0") and e.state["publication_rows"] == 1


@pytest.mark.parametrize("field,value", [("user_busy", True), ("cc_busy", True),
    ("generation", 3), ("goal", None), ("goal", 12), ("goal", 10),
    ("enabled", False), ("adopted", False), ("epoch_ok", False)])
async def test_publication_followup_busy_or_stale_does_not_extend_or_drop_goal(followup, field, value):
    e = followup
    e.state[field] = value
    goal = e.state["goal"]
    assert not await repo.reserve_remaining_pressure(e.session, user_id=e.uid, generation=2,
                                                    published_through_id=12, privacy_epoch=4)
    assert e.state["queued"] == [] and e.state["goal"] == goal
    assert e.state["settings"] == ("0", "0") and e.state["publication_rows"] == 1


async def test_optional_followup_error_rolls_back_only_savepoint_and_keeps_publication(followup, caplog):
    e = followup
    e.state["error"] = True
    assert not await repo.reserve_remaining_pressure(e.session, user_id=e.uid, generation=2,
                                                    published_through_id=12, privacy_epoch=4)
    assert e.state["publication_rows"] == 1 and e.state["goal"] == 20 and not e.state["queued"]
    assert "rollback_savepoint" in e.events and "synthetic-private-error" not in caplog.text
    assert e.state["settings"] == ("0", "0")


def test_followup_uses_only_nonblocking_lock_forms_and_published_generation_evidence():
    assert "pg_try_advisory_xact_lock" in str(repo._TRY_PRESSURE_USER)
    sql = str(repo._REMAINING_PRESSURE_GOAL)
    assert "FOR UPDATE OF cc SKIP LOCKED" in sql
    assert "cc.memory_generation=:generation" in sql
    assert "retention_pressure_through_message_id>:published_through" in sql
    assert "processing_complete" in sql


@pytest.mark.parametrize("terminal_epoch,expected", [(4, True), (3, False), (None, False)])
async def test_retention_contract_first_and_resumed_jobs_use_memory_max_three_without_epoch_rebind(monkeypatch, terminal_epoch, expected):
    uid, made = uuid.uuid4(), []
    monkeypatch.setattr(retention, "capture_privacy_epoch", AsyncMock(return_value=4))
    monkeypatch.setattr(retention, "privacy_epoch_matches", AsyncMock(return_value=True))
    payload = {"retention_enabled": True, "memory_generation": 2, "through_message_id": 12,
               "privacy_epoch": terminal_epoch, "after_message_id": 0}
    class Session:
        async def scalar(self, sql, params):
            return None if sql is retention._CONTRACT_ACTIVE else 0
        async def execute(self, sql, params):
            if sql is retention._CONTRACT_LATEST_TERMINAL:
                return Result(("succeeded",))
            if sql is retention._CONTRACT_BASE_JOB:
                return Result((uuid.uuid4(),))
            assert sql is retention._CONTRACT_UNPROCESSED_TERMINAL
            return Result((uuid.uuid4(), payload))
    async def enqueue(session, **kwargs):
        made.append(kwargs)
        return uuid.uuid4()
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    result = await retention.enqueue_contract_processing(Session(), user_id=uid, generation=2, through_id=12)
    assert (result is not None) is expected
    if expected:
        assert made[0]["queue"] == jobs.QUEUE_MEMORY and made[0]["max_attempts"] == 3
        assert made[0]["payload"]["privacy_epoch"] == 4
    assert payload["privacy_epoch"] == terminal_epoch


async def test_new_retention_contract_binds_epoch_and_memory_budget_on_first_reservation(monkeypatch):
    uid, made = uuid.uuid4(), []
    monkeypatch.setattr(retention, "capture_privacy_epoch", AsyncMock(return_value=4))
    monkeypatch.setattr(retention, "privacy_epoch_matches", AsyncMock(return_value=True))
    class Session:
        async def scalar(self, sql, params):
            return None if sql is retention._CONTRACT_ACTIVE else 0
        async def execute(self, sql, params):
            return Result()
    async def enqueue(session, **kwargs):
        made.append(kwargs)
        return uuid.uuid4()
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    assert await retention.enqueue_contract_processing(Session(), user_id=uid, generation=2, through_id=12)
    assert made[0]["queue"] == jobs.QUEUE_MEMORY and made[0]["max_attempts"] == 3
    assert made[0]["payload"] == {"retention_enabled": True, "memory_generation": 2,
        "through_message_id": 12, "after_message_id": 0, "privacy_epoch": 4}


async def test_one_page_off_then_sweep_resumes_same_completed_parent_without_model(env):
    from dataclasses import replace
    from worker import checkpoint_jobs as worker, memory_sweep_jobs as sweep
    result = await worker.handle_checkpoint(env.job)
    env.enabled.return_value = False
    await result.apply_domain(env.session)
    payload = env.archived.await_args.args[2]
    assert result.result_code == "checkpoint_draft_ready" and payload["mode"] == "publish_retention"
    env.enabled.return_value = True
    env.model.reset_mock()
    env.enqueue.reset_mock()
    plan = checkpoint.plan(env.sources, previous=None, memory_generation=2,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION, reason="input_pressure", through_message_id=2)
    plan = replace(plan, privacy_epoch=3)
    class ResumeSession:
        async def execute(self, sql, params):
            assert sql is sweep._RETENTION_UNPUBLISHED_SUCCESS
            assert "j.result_code='checkpoint_draft_ready' AND j.payload->>'mode'='publish_retention'" in str(sql)
            return Result((env.job.id, payload, 3, 1))
    assert await sweep._resume_unpublished_success(ResumeSession(), user_id=env.job.user_id,
        generation=2, plan=plan, previous=None)
    resumed_payload = env.enqueue.await_args.kwargs["payload"]
    assert resumed_payload == payload
    resumed = SimpleNamespace(**{**vars(env.job), "id": uuid.uuid4(), "payload": resumed_payload})
    publication = await worker.handle_checkpoint(resumed)
    await publication.apply_domain(env.session)
    env.model.assert_not_awaited()
    env.inserted.assert_awaited_once()
