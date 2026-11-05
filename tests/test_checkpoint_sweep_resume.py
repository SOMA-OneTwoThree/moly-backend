"""추가 발화 없이 후속 요약을 예약한다. 잡 확정과 사용자 예약의 잠금을 분리한다."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest

from app.config import settings
from app.services import checkpoint, context_retention
from worker import memory_sweep_jobs as sweep


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


def message(mid, sender="user", kind="normal", size=1000):
    return checkpoint.SourceMessage(mid, sender, kind, "x" * size, datetime.now(timezone.utc))


def previous(through=13):
    return checkpoint.Checkpoint(
        uuid.uuid4(), through, "", checkpoint.RETENTION_SUMMARIZER_VERSION,
        "old-source", state_json={"version": 1, "sequence": 1, "items": []},
    )


def install(monkeypatch, *, pending=41, checkpoint_value=None, messages=None, busy=False,
            stale=False, enabled=True, second_enabled=True, made=True, dead=None, fail_commit=False,
            terminal=None, adopted=True, pressure=None, plan_terminal=None):
    uid = uuid.uuid4()
    history = messages or [message(27), message(37, "moly", "safety"), message(41), message(55, "moly")]
    checkpoint_value = checkpoint_value or previous()
    events, calls = [], []
    counts = {"sessions": 0}

    class Session:
        def __init__(self, name):
            self.name = name

        async def __aenter__(self):
            events.append((self.name, "enter"))
            return self

        async def __aexit__(self, *args):
            events.append((self.name, "exit"))

        async def scalar(self, sql, params):
            assert sql is sweep._TRY_DOCUMENT_USER_LOCK
            events.append((self.name, "try_user_lock"))
            assert params == {"user_id": str(uid)}
            return not busy

        async def execute(self, sql, params):
            if sql is sweep._RETENTION_RESUME_USERS:
                events.append((self.name, "scan"))
                return Result([(uid, 7)])
            if sql is sweep._RETENTION_CURRENT:
                events.append((self.name, "current"))
                assert params == {"user_id": uid, "generation": 7}
                return Result([] if stale else [(7, pending, 2, 3, history[-1].id, pressure)])
            if sql is sweep._CLEAR_RETENTION_TARGET:
                events.append((self.name, "clear_target"))
                assert params["generation"] == 7
                assert params["plan_revision"] == 2 and params["anchor_revision"] == 3
                complete = not any(checkpoint_value.through_message_id < m.id < pending for m in history)
                return Result([(uid,)] if complete else [])
            if sql is sweep._RETENTION_DEAD_PLAN:
                events.append((self.name, "dead_plan"))
                calls.append(("dead", params))
                return Result([(dead,)] if dead else [])
            if sql is sweep._RETENTION_UNPUBLISHED_SUCCESS:
                calls.append(("unpublished", params))
                return Result([terminal] if terminal else [])
            if sql is sweep._RETENTION_PLAN_LATEST_TERMINAL:
                calls.append(("plan_terminal", params))
                return Result([plan_terminal] if plan_terminal else [])
            raise AssertionError("unexpected query")

        async def commit(self):
            events.append((self.name, "commit"))
            if fail_commit:
                raise RuntimeError("simulated commit failure")

    def factory():
        counts["sessions"] += 1
        return Session("scan" if counts["sessions"] == 1 else "user")

    async def flag(session, *, lock=False):
        events.append((session.name, "flag_share" if lock else "flag_read"))
        return enabled if session.name == "scan" else second_enabled

    async def load_latest(session, user_id, **kwargs):
        assert user_id == uid and kwargs == {"retention_enabled": True}
        events.append((session.name, "latest"))
        return checkpoint_value

    async def no_compat_baseline(*args, **kwargs):
        return None

    async def load_range(session, user_id, **kwargs):
        assert user_id == uid and kwargs["include_excluded"] is True
        events.append((session.name, "source_page"))
        calls.append(("page", kwargs))
        return [m for m in history if kwargs["after_id"] < m.id <= kwargs["through_id"]][:kwargs["max_rows"]]

    async def enqueue(session, **kwargs):
        events.append((session.name, "enqueue"))
        calls.append(("enqueue", kwargs))
        return uuid.uuid4() if made else None

    async def replay(session, **kwargs):
        events.append((session.name, "replay"))
        calls.append(("replay", kwargs))
        return uuid.uuid4()

    async def cohort(*args, **kwargs):
        return adopted
    async def epoch(*args, **kwargs):
        return kwargs.get("privacy_epoch") == 0

    async def resume_enqueue(session, **kwargs):
        calls.append(("resume_enqueue", kwargs))
        return uuid.uuid4()

    async def redo_enqueue(session, **kwargs):
        calls.append(("redo_enqueue", kwargs))
        return uuid.uuid4()

    async def cursor_read(session):
        return None

    async def cursor_advance(**kwargs):
        # Cursor writes must run after the per-user session has released its lock.
        assert events[-1] == ("user", "exit")
        return True

    async def resolve_release(session):
        return None

    monkeypatch.setattr(sweep.retention_release, "resolve", resolve_release)
    monkeypatch.setattr(settings, "context_checkpoint_enabled", True)
    monkeypatch.setattr(sweep, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(sweep, "_retention_enabled", flag)
    monkeypatch.setattr(sweep.checkpoint_repo, "load_latest", load_latest)
    monkeypatch.setattr(sweep.checkpoint_repo, "load_compat_baseline", no_compat_baseline)
    monkeypatch.setattr(sweep.checkpoint_repo, "load_range", load_range)
    monkeypatch.setattr(sweep.checkpoint_repo, "maybe_enqueue", enqueue)
    monkeypatch.setattr(sweep.checkpoint_repo, "enqueue_checkpoint", redo_enqueue)
    monkeypatch.setattr(sweep.jobs, "replay_dead", replay)
    monkeypatch.setattr(sweep.jobs, "enqueue", resume_enqueue)
    monkeypatch.setattr(sweep.context_retention, "adopted_user", cohort)
    monkeypatch.setattr(sweep.context_retention, "privacy_epoch_matches", epoch)
    monkeypatch.setattr(sweep, "_read_resume_cursor", cursor_read)
    monkeypatch.setattr(sweep, "_advance_resume_cursor", cursor_advance)
    return SimpleNamespace(uid=uid, events=events, calls=calls, counts=counts, previous=checkpoint_value)


async def test_running_job_followup_is_reserved_without_another_chat_message(monkeypatch):
    setup = install(monkeypatch)
    result = await sweep._resume_retention_checkpoints()
    assert result == sweep.RetentionResumeResult(candidates=1, enqueued=1)
    reserve = next(data for name, data in setup.calls if name == "enqueue")
    assert [m.id for m in reserve["messages"]] == [27, 37, 41, 55]
    assert reserve["messages"][1].kind == "safety"  # coverage·hash에는 보존, 모델 입력에서는 별도 제외
    assert reserve["keep_from_message_id"] == 41
    assert reserve["retention_enabled"] is True and reserve["reset_triggered"] is True
    assert setup.events.index(("scan", "exit")) < setup.events.index(("user", "enter"))
    assert setup.events.index(("user", "try_user_lock")) < setup.events.index(("user", "enqueue"))
    assert ("user", "flag_share") in setup.events
    assert setup.events[-2:] == [("user", "commit"), ("user", "exit")]


async def test_missing_target_with_enough_unprocessed_source_is_reserved(monkeypatch):
    setup = install(monkeypatch, pending=None)
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 1
    reserve = next(data for name, data in setup.calls if name == "enqueue")
    assert reserve["reset_triggered"] is False
    assert not any(event == "clear_target" for _session, event in setup.events)


async def test_already_covered_target_is_cleared_with_generation_and_revision(monkeypatch):
    setup = install(monkeypatch, pending=14)
    assert (await sweep._resume_retention_checkpoints()).enqueued == 1
    reserve = next(data for name, data in setup.calls if name == "enqueue")
    assert reserve["reset_triggered"] is False
    assert ("user", "clear_target") in setup.events


async def test_older_small_pending_target_can_expand_when_new_source_is_ready(monkeypatch):
    setup = install(monkeypatch, pending=28)
    assert (await sweep._resume_retention_checkpoints()).enqueued == 1
    reserve = next(data for name, data in setup.calls if name == "enqueue")
    assert reserve["keep_from_message_id"] == 41  # 28에 묶여 최소 신규 구간 미달을 반복하지 않음


async def test_pending_work_below_normal_trigger_still_resumes_minimum_head(monkeypatch):
    history = [message(27, size=1000), message(37, "moly", size=200), message(41, size=800)]
    setup = install(monkeypatch, pending=41, messages=history)
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 1
    reserve = next(data for name, data in setup.calls if name == "enqueue")
    assert reserve["keep_from_message_id"] == 41 and reserve["reset_triggered"] is True


async def test_too_small_pending_head_is_not_marked_complete_or_queued(monkeypatch):
    setup = install(monkeypatch, pending=41, messages=[message(27, size=10), message(41, size=200)])
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 0
    assert not any(name in {"enqueue", "replay"} for name, _data in setup.calls)
    assert ("user", "commit") in setup.events


@pytest.mark.parametrize(("kwargs", "expected_event"), [
    ({"busy": True}, "try_user_lock"),
    ({"stale": True}, "current"),
    ({"second_enabled": False}, "flag_share"),
])
async def test_busy_stale_deleted_or_disabled_users_are_not_reserved(monkeypatch, kwargs, expected_event):
    setup = install(monkeypatch, **kwargs)
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == result.replayed == 0
    assert not setup.calls
    assert ("user", expected_event) in setup.events


async def test_disabled_feature_does_not_scan_or_take_user_locks(monkeypatch):
    setup = install(monkeypatch, enabled=False)
    assert await sweep._resume_retention_checkpoints() == sweep.RetentionResumeResult()
    assert setup.counts["sessions"] == 1
    assert setup.events == [("scan", "enter"), ("scan", "flag_read"), ("scan", "exit")]


async def test_retention_sweep_runs_when_legacy_checkpoint_flag_is_off(monkeypatch):
    setup = install(monkeypatch)
    monkeypatch.setattr(settings, "context_checkpoint_enabled", False)
    assert (await sweep._resume_retention_checkpoints()).enqueued == 1
    assert any(name == "enqueue" for name, _kwargs in setup.calls)


async def test_invalid_verified_checkpoint_is_preserved_not_overwritten(monkeypatch):
    setup = install(monkeypatch)
    async def invalid(*args, **kwargs):
        raise context_retention.RetentionError("checkpoint_not_validated")
    monkeypatch.setattr(sweep.checkpoint_repo, "load_latest", invalid)
    result = await sweep._resume_retention_checkpoints()
    assert result.blocked == 1 and result.enqueued == 0
    assert not setup.calls and ("user", "commit") not in setup.events


async def test_commit_failure_does_not_report_a_completed_reservation(monkeypatch):
    setup = install(monkeypatch, fail_commit=True)
    result = await sweep._resume_retention_checkpoints()
    assert result.blocked == 1 and result.enqueued == result.replayed == 0
    assert any(name == "enqueue" for name, _kwargs in setup.calls)


def just_died(job_id, code="summary_invalid", *, ago=timedelta(minutes=1), depth=None):
    return ("dead", job_id, datetime.now(timezone.utc) - ago, code,
            None if depth is None else str(depth))


async def test_exact_current_dead_plan_is_blocked_without_a_new_attempt_budget(monkeypatch):
    dead = uuid.uuid4()
    setup = install(monkeypatch, made=False, dead=dead, plan_terminal=just_died(dead))
    result = await sweep._resume_retention_checkpoints()
    assert result.replayed == result.enqueued == 0 and result.blocked == 1
    criteria = next(data for name, data in setup.calls if name == "dead")
    assert criteria["user_id"] == setup.uid and criteria["generation"] == 7
    assert criteria["through_id"] == 37
    assert criteria["previous_id"] == str(setup.previous.id)
    assert checkpoint.RETENTION_SUMMARIZER_VERSION in criteria["dedup_key"]
    assert not any(name in {"replay", "unpublished", "resume_enqueue", "redo_enqueue"}
                   for name, _data in setup.calls)
    assert ("user", "commit") in setup.events


async def test_repeated_sweeps_do_not_create_new_jobs_or_reset_dead_attempts(monkeypatch):
    dead = uuid.uuid4()
    setup = install(monkeypatch, made=False, dead=dead, plan_terminal=just_died(dead))
    for _ in range(4):
        result = await sweep._resume_retention_checkpoints()
        assert result.blocked == 1 and result.enqueued == result.replayed == 0
    assert len([data for name, data in setup.calls if name == "dead"]) == 4
    assert not any(name in {"replay", "unpublished", "resume_enqueue", "redo_enqueue"}
                   for name, _data in setup.calls)


async def test_dead_plan_is_recreated_as_a_fresh_job_after_its_interval(monkeypatch):
    dead = uuid.uuid4()
    setup = install(monkeypatch, made=False, dead=dead,
                    plan_terminal=just_died(dead, ago=timedelta(minutes=31)))
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 1 and result.blocked == 0
    lookup = next(data for name, data in setup.calls if name == "plan_terminal")
    redo = next(data for name, data in setup.calls if name == "redo_enqueue")
    assert lookup["source_hash"] == redo["plan"].source_hash and lookup["generation"] == 7
    assert redo["redo"] == (str(dead), 1) and redo["user_id"] == setup.uid
    assert not any(name in {"replay", "resume_enqueue"} for name, _data in setup.calls)


async def test_dead_progress_page_blocks_resume_but_is_recreated_after_its_interval(monkeypatch):
    # 원본은 성공하고 다음 쪽 작업이 dead면 _RETENTION_DEAD_PLAN에는 안 잡히고 이어받기도 막힌다.
    page = uuid.uuid4()
    setup = install(monkeypatch, made=False,
                    plan_terminal=just_died(page, ago=timedelta(hours=3), depth=2))
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 1
    redo = next(data for name, data in setup.calls if name == "redo_enqueue")
    assert redo["redo"] == (str(page), 3)


async def test_unpublished_success_of_a_redo_after_the_dead_root_is_resumed(monkeypatch):
    # 원본은 dead로 남아 있어도(재생성 작업은 replay_of로 이어지지 않는다) 그 뒤 다시 만든
    # 작업이 성공으로 끝났으면 그 durable 상태에서 이어받는다.
    old, history, plan = resume_plan()
    redo_id = uuid.uuid4()
    payload = {**plan.payload(), **{"redo_of": str(uuid.uuid4()), "redo_depth": 1},
               "progress_state": context_retention.empty_state(),
               "progress_next_chunk": 2, "progress_manifest": "c"*64}
    setup = install(monkeypatch, checkpoint_value=old, messages=history, made=False,
        dead=uuid.uuid4(), terminal=(redo_id, payload),
        plan_terminal=("succeeded", redo_id, datetime.now(timezone.utc), None, "1"))
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 1 and result.blocked == 0
    queued = next(value for name, value in setup.calls if name == "resume_enqueue")
    assert queued["dedup_key"] == f"checkpoint-resume:{redo_id}" and queued["payload"] == payload
    assert not any(name == "redo_enqueue" for name, _data in setup.calls)


@pytest.mark.parametrize("code", ["invalid_payload", "checkpoint_completed_proof_invalid"])
async def test_other_dead_codes_keep_the_durable_stop(monkeypatch, code):
    dead = uuid.uuid4()
    setup = install(monkeypatch, made=False, dead=dead,
                    plan_terminal=just_died(dead, code, ago=timedelta(days=2)))
    result = await sweep._resume_retention_checkpoints()
    assert result.blocked == 1 and result.enqueued == 0
    assert not any(name == "redo_enqueue" for name, _data in setup.calls)


@pytest.mark.parametrize("code", ["summary_name_leak", "summary_length", "summary_packing_length_snapshot"])
async def test_content_failure_codes_from_the_handler_are_recreated(monkeypatch, code):
    # 요약 처리기가 재시도 끝에 이 코드로 dead를 남기면 예전에는 그 사용자의 요약이 영구히 멈췄다.
    dead = uuid.uuid4()
    setup = install(monkeypatch, made=False, dead=dead,
                    plan_terminal=just_died(dead, code, ago=timedelta(minutes=31)))
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 1 and result.blocked == 0
    redo = next(data for name, data in setup.calls if name == "redo_enqueue")
    assert redo["redo"] == (str(dead), 1)


@pytest.mark.parametrize("depth,wait", [(None, timedelta(minutes=30)), (1, timedelta(hours=1)),
                                        (2, timedelta(hours=2)), (3, timedelta(hours=4))])
async def test_lineage_is_recreated_with_growing_intervals_up_to_the_cap(monkeypatch, depth, wait):
    dead = uuid.uuid4()
    early = install(monkeypatch, made=False, dead=dead,
                    plan_terminal=just_died(dead, ago=wait - timedelta(minutes=1), depth=depth))
    assert (await sweep._resume_retention_checkpoints()).enqueued == 0
    assert not any(name == "redo_enqueue" for name, _data in early.calls)
    setup = install(monkeypatch, made=False, dead=dead,
                    plan_terminal=just_died(dead, ago=wait, depth=depth))
    assert (await sweep._resume_retention_checkpoints()).enqueued == 1
    redo = next(data for name, data in setup.calls if name == "redo_enqueue")
    assert redo["redo"] == (str(dead), (depth or 0) + 1)


@pytest.mark.parametrize("dead_root", [True, False])
async def test_lineage_stops_at_the_cap_and_logs_only_ids(monkeypatch, caplog, dead_root):
    capped = uuid.uuid4()
    setup = install(monkeypatch, made=False, dead=uuid.uuid4() if dead_root else None,
                    plan_terminal=just_died(capped, "summary_invalid", ago=timedelta(days=3), depth=4))
    with caplog.at_level("WARNING", logger="moly-worker"):
        result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 0 and result.blocked == 1
    assert not any(name == "redo_enqueue" for name, _data in setup.calls)
    logged = [record.getMessage() for record in caplog.records if "checkpoint_redo_exhausted" in record.getMessage()]
    assert logged == [f"checkpoint_redo_exhausted dead_job={capped} error_code=summary_invalid redo_depth=4"]


def test_plan_terminal_lookup_reads_only_the_same_plan_and_changes_nothing():
    sql = str(sweep._RETENTION_PLAN_LATEST_TERMINAL)
    assert sql.strip().startswith("SELECT j.state,j.id,j.finished_at,j.last_error_code,j.payload->>'redo_depth'")
    assert "j.payload->>'source_hash'=:source_hash" in sql
    assert "cc.memory_generation=:generation" in sql
    assert "ORDER BY j.finished_at DESC,j.id DESC LIMIT 1" in sql
    assert "UPDATE" not in sql and "DELETE" not in sql


async def test_no_unresolved_dead_plan_means_no_replay(monkeypatch):
    setup = install(monkeypatch, made=False)
    result = await sweep._resume_retention_checkpoints()
    assert result.replayed == result.enqueued == 0
    assert not any(name == "replay" for name, _kwargs in setup.calls)


def resume_plan(old=None):
    old = old or previous()
    history = [message(27), message(37, "moly", "safety"), message(41), message(55, "moly")]
    plan = checkpoint.plan(history, previous=old, keep_from_message_id=41,
        memory_generation=7, reset_triggered=True, version=checkpoint.RETENTION_SUMMARIZER_VERSION)
    plan = replace(plan, privacy_epoch=0)
    return old, history, plan


async def test_reenable_resumes_unpublished_success_with_exact_continuation_payload(monkeypatch):
    old, history, plan = resume_plan()
    terminal_id = uuid.uuid4()
    payload = {**plan.payload(), "progress_state": context_retention.empty_state(),
               "progress_next_chunk": 3, "progress_manifest": "a"*64}
    setup = install(monkeypatch, checkpoint_value=old, messages=history, made=False,
        terminal=(terminal_id, payload))
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 1 and result.replayed == 0
    criteria = next(value for name, value in setup.calls if name == "unpublished")
    assert criteria["source_hash"] == plan.source_hash and criteria["through_id"] == plan.through_message_id
    queued = next(value for name, value in setup.calls if name == "resume_enqueue")
    assert queued["dedup_key"] == f"checkpoint-resume:{terminal_id}"
    assert queued["payload"] == payload  # 앞 조각을 재실행하지 않고 마지막 durable 상태에서 재개


async def test_repeated_off_on_uses_each_new_disabled_terminal_without_mutating_history(monkeypatch):
    old, history, plan = resume_plan()
    original_payload = {**plan.payload(), "progress_state": context_retention.empty_state(),
                        "progress_next_chunk": 2, "progress_manifest": "b"*64}
    keys = []
    for _ in range(3):
        disabled_terminal = uuid.uuid4()
        setup = install(monkeypatch, checkpoint_value=old, messages=history, made=False,
            terminal=(disabled_terminal, original_payload), second_enabled=False)
        assert (await sweep._resume_retention_checkpoints()).enqueued == 0
        assert not setup.calls
        setup = install(monkeypatch, checkpoint_value=old, messages=history, made=False,
            terminal=(disabled_terminal, original_payload))
        assert (await sweep._resume_retention_checkpoints()).enqueued == 1
        queued = next(value for name, value in setup.calls if name == "resume_enqueue")
        keys.append(queued["dedup_key"])
        assert queued["payload"] == original_payload
    assert len(set(keys)) == 3 and original_payload["progress_next_chunk"] == 2


async def test_published_success_is_not_scheduled_again(monkeypatch):
    old, history, plan = resume_plan()
    published = previous(through=plan.through_message_id)
    setup = install(monkeypatch, checkpoint_value=published, messages=history,
        terminal=(uuid.uuid4(), plan.payload()))
    assert (await sweep._resume_retention_checkpoints()).enqueued == 0
    assert not any(name in {"unpublished", "resume_enqueue"} for name, _value in setup.calls)


async def test_mismatched_durable_plan_is_held_without_reserving_new_work(monkeypatch):
    old, history, plan = resume_plan()
    payload = {**plan.payload(), "source_message_ids": [27]}
    setup = install(monkeypatch, checkpoint_value=old, messages=history, made=False,
        terminal=(uuid.uuid4(), payload))
    result = await sweep._resume_retention_checkpoints()
    assert result.blocked == 1 and result.enqueued == 0
    assert not any(name == "resume_enqueue" for name, _value in setup.calls)


async def test_ready_cohort_removal_prevents_recovery(monkeypatch):
    setup = install(monkeypatch, adopted=False)
    assert (await sweep._resume_retention_checkpoints()).enqueued == 0
    assert not setup.calls


async def test_two_simultaneous_sweeps_reserve_only_one_resumed_payload(monkeypatch):
    import asyncio
    old, history, plan = resume_plan()
    setup = install(monkeypatch, checkpoint_value=old, messages=history, made=False,
        terminal=(uuid.uuid4(), plan.payload()))
    factory = sweep.get_sessionmaker()
    held = False
    class ConcurrentSession:
        def __init__(self):
            self.inner, self.owns = factory(), False
        @property
        def name(self):
            return self.inner.name
        async def __aenter__(self):
            await self.inner.__aenter__()
            return self
        async def __aexit__(self, *args):
            nonlocal held
            if self.owns:
                held = False
            await self.inner.__aexit__(*args)
        async def scalar(self, sql, params):
            nonlocal held
            if held:
                return False
            held, self.owns = True, True
            await asyncio.sleep(0)  # 첫 예약 트랜잭션이 열린 동안 두 번째 sweep를 진입시킨다.
            return await self.inner.scalar(sql, params)
        async def execute(self, *args):
            return await self.inner.execute(*args)
        async def commit(self):
            return await self.inner.commit()
    monkeypatch.setattr(sweep, "get_sessionmaker", lambda: ConcurrentSession)
    results = await asyncio.gather(sweep._resume_retention_checkpoints(), sweep._resume_retention_checkpoints())
    assert sum(result.enqueued for result in results) == 1
    assert sum(result.busy for result in results) == 1
    assert len([value for name, value in setup.calls if name == "resume_enqueue"]) == 1


def test_unpublished_success_lookup_protects_published_and_active_plans_without_changing_history():
    sql = str(sweep._RETENTION_UNPUBLISHED_SUCCESS)
    assert "j.state='succeeded'" in sql and "c.through_message_id>=:through_id" in sql
    assert "active_job.state IN ('ready','running')" in sql
    assert "j.payload->>'source_hash'=:source_hash" in sql
    assert "cc.memory_generation=:generation" in sql and ")=:previous_id" in sql
    assert "failed.state='dead'" in sql and "failed.finished_at,failed.id" in sql
    assert "UPDATE async_jobs" not in sql and "DELETE" not in sql


async def test_source_pages_are_continuous_and_include_policy_exclusions(monkeypatch):
    source = [message(10 + i * 2, "user" if i % 2 else "moly", size=1) for i in range(260)]
    setup = install(monkeypatch, messages=source)
    result = await sweep._load_retention_segment(
        SimpleNamespace(name="paging"), setup.uid, after_id=0, through_id=source[-1].id,
    )
    assert result == source
    pages = [data for name, data in setup.calls if name == "page"]
    assert len(pages) == 3 and all(page["max_rows"] == 120 for page in pages)
    assert [page["after_id"] for page in pages] == [0, source[119].id, source[239].id]
    assert all(page["include_excluded"] for page in pages)


async def test_bad_or_nonadvancing_page_does_not_silently_skip_source(monkeypatch):
    async def load(*args, **kwargs):
        return [message(11), message(10)]
    monkeypatch.setattr(sweep.checkpoint_repo, "load_range", load)
    with pytest.raises(context_retention.RetentionError):
        await sweep._load_retention_segment(None, uuid.uuid4(), after_id=0, through_id=20)


async def test_recovery_happens_before_sweep_finalize_and_needs_no_apply_domain(monkeypatch):
    events = []
    class ScanSession:
        async def __aenter__(self):
            events.append("scan_enter")
            return self
        async def __aexit__(self, *args):
            events.append("scan_exit")
        async def execute(self, *args, **kwargs):
            return Result()
        async def scalar(self, *args, **kwargs):
            return None
    async def resume():
        assert events == ["scan_enter", "scan_exit"]
        events.append("independent_user_reservation")
        return sweep.RetentionResumeResult(candidates=1, enqueued=1)
    monkeypatch.setattr(sweep, "get_sessionmaker", lambda: ScanSession)
    monkeypatch.setattr(sweep, "_resume_retention_checkpoints", resume)
    result = await sweep.handle_memory_sweep(None)
    assert result.result_code == "ok" and result.apply_domain is None
    assert result.result_detail["checkpoint_enqueued"] == 1
    assert events[-1] == "independent_user_reservation"


def test_replay_lookup_excludes_all_successful_descendants_and_current_work():
    sql = str(sweep._RETENTION_DEAD_PLAN)
    assert "WITH RECURSIVE replay_chain" in sql
    assert "child.replay_of=parent.id" in sql
    assert "done.state IN ('ready','running','succeeded')" in sql
    assert "j.dedup_key=:dedup_key" in sql and "cc.memory_generation=:generation" in sql
    assert "c.through_message_id>=:through_id" in sql
    assert "COALESCE(j.payload->>'mode','normal')='normal'" in sql
    assert "UPDATE async_jobs" not in sql


def test_discovery_uses_real_source_size_and_privacy_not_dead_row_count():
    sql = str(sweep._RETENTION_RESUME_USERS)
    assert "sum(COALESCE(CASE WHEN m.content_ciphertext IS NULL THEN octet_length(m.content) ELSE octet_length(m.content_ciphertext)-32 END,0)+24)>=4096" in sql
    assert "privacy_subject_barriers" in sql and "b.state<>'active'" in sql
    assert "'conversation-checkpoint-summary-v5'" in sql
    assert "interval '60 seconds'" in sql and "LIMIT :limit" in sql
    assert "state='dead'" not in sql


async def test_completed_publish_draft_resumes_verbatim_with_remaining_budget_without_model_reentry(monkeypatch):
    old, history, plan = resume_plan()
    terminal_id = uuid.uuid4()
    payload = {**plan.payload(), "mode": "publish_retention",
               "completed_state": context_retention.empty_state(),
               "completed_proof": {"next_chunk": 3, "total_chunks": 3},
               "completed_binding": "a"*64}
    pressure_plan = replace(plan, reason="input_pressure")
    setup = install(monkeypatch, checkpoint_value=old, messages=history,
        terminal=(terminal_id, payload, 3, 2))
    session = sweep.get_sessionmaker()()
    made = await sweep._resume_unpublished_success(session,
        user_id=setup.uid, generation=7, plan=pressure_plan, previous=old)
    assert made is not None
    queued = next(value for name, value in setup.calls if name == "resume_enqueue")
    assert queued["payload"] == payload  # reason is part of the completed binding
    assert queued["max_attempts"] == 2
    assert queued["payload"]["mode"] == "publish_retention"


async def test_unbound_completed_draft_never_acquires_a_fresh_epoch(monkeypatch):
    old, history, plan = resume_plan()
    payload = {**plan.payload(), "mode": "publish_retention", "privacy_epoch": 9}
    setup = install(monkeypatch, checkpoint_value=old, messages=history, terminal=(uuid.uuid4(), payload))
    session = sweep.get_sessionmaker()()
    assert await sweep._resume_unpublished_success(session,
        user_id=setup.uid, generation=7, plan=plan, previous=old) is None
    assert not any(name == "resume_enqueue" for name, _ in setup.calls)
    assert payload["privacy_epoch"] == 9


def test_completed_draft_selector_precedes_model_pages_and_only_accepts_materialized_draft_ready():
    sql = str(sweep._RETENTION_UNPUBLISHED_SUCCESS)
    assert "IN ('normal','publish_retention')" in sql
    assert "ORDER BY (j.payload->>'mode'='publish_retention') DESC" in sql
    assert "j.result_code='checkpoint_draft_ready' AND j.payload->>'mode'='publish_retention'" in sql
    assert "failed.payload->>'mode','normal') IN ('normal','publish_retention')" in sql


async def test_sweep_captures_active_projection_even_when_parent_job_is_legacy(monkeypatch):
    from app.services.retention_preparation_context import Binding, current_binding, use_binding
    identity = uuid.uuid4()
    active = Binding(identity, False)
    statements = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement, params):
            assert current_binding() == active
            statements.append(str(statement))
            assert params['preparation_id'] == identity
            assert params['preparation_building'] == 'false'
            assert 'c.preparation_id=CAST(:preparation_id AS uuid)' in str(statement)
            assert "j.payload->>'retention_preparation_id'" in str(statement)
            return Result()

    async def resolve(session):
        # The sweep starts a fresh producer scope, independent of the parent.
        assert current_binding() is None
        return active

    async def flag(session, **kwargs):
        assert current_binding() == active
        return True

    async def cursor(session):
        return None

    monkeypatch.setattr(sweep, 'get_sessionmaker', lambda: Session)
    monkeypatch.setattr(sweep.retention_release, 'resolve', resolve)
    monkeypatch.setattr(sweep, '_retention_enabled', flag)
    monkeypatch.setattr(sweep, '_read_resume_cursor', cursor)
    with use_binding(None):
        assert await sweep._resume_retention_checkpoints() == sweep.RetentionResumeResult()
        assert current_binding() is None
    assert len(statements) == 1
