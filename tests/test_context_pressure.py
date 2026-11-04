"""입력 상한 복구: 저장 경계·고정 작업·세대·후속 처리·커서의 오프라인 회귀."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import checkpoint, checkpoint_repo as repo, context_retention
from worker import memory_sweep_jobs as sweep
from tests.test_checkpoint_sweep_resume import install, message, Result


def pressure_plan(messages, *, previous=None, through=None):
    return checkpoint.plan(messages, previous=previous,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION, memory_generation=7,
        through_message_id=through or messages[-1].id, reason="input_pressure")


@pytest.mark.parametrize("sizes", [(1850, 500), (1, 1), (30000, 500)])
def test_pressure_includes_latest_complete_turn_even_when_normal_plan_has_no_head(sizes):
    sources = [message(1, size=sizes[0]), message(2, "moly", size=sizes[1])]
    tail = checkpoint.keep_tail(sources, retention_enabled=True)
    assert checkpoint.plan(sources, previous=None, keep_from_message_id=tail[0].id,
        reset_triggered=True, version=checkpoint.RETENTION_SUMMARIZER_VERSION) is None
    plan = pressure_plan(sources)
    assert plan.source_message_ids == (1, 2) and plan.through_message_id == 2
    assert plan.payload()["reason"] == "input_pressure"


def test_pressure_next_page_uses_current_parent_and_only_uncovered_sources():
    sources = [message(i, "moly" if i % 2 == 0 else "user") for i in range(1, 7)]
    parent = checkpoint.Checkpoint(uuid.uuid4(), 2, "", checkpoint.RETENTION_SUMMARIZER_VERSION, "x")
    plan = pressure_plan(sources, previous=parent, through=4)
    assert plan.source_message_ids == (3, 4) and plan.previous_id == parent.id
    assert pressure_plan(sources[:2], previous=parent) is None


@pytest.mark.parametrize("through", [0, -1, True, 3])
def test_pressure_rejects_non_source_boundary(through):
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.plan([message(1), message(2, "moly")], previous=None,
            version=checkpoint.RETENTION_SUMMARIZER_VERSION,
            through_message_id=through, reason="input_pressure")


@pytest.fixture
def reservation(monkeypatch):
    uid = uuid.uuid4()
    state = {"enabled": True, "adopted": True, "boundary": 2, "target": None,
             "generation": 7, "active": None, "queued": [], "queries": []}
    async def initialized_baseline(*args, **kwargs):
        if state.get("previous") is not None:
            return None
        return SimpleNamespace(source_after=0, payload=lambda: {
            "source_after": 0, "generation": 7, "privacy_epoch": 0})
    monkeypatch.setattr(repo, "load_compat_baseline", initialized_baseline)
    sources = [message(1, size=10), message(2, "moly", size=10)]
    class Session:
        async def scalar(self, sql, params):
            state["queries"].append((sql, params))
            if sql is repo._PRESSURE_BOUNDARY:
                return state["boundary"] if params["generation"] == state["generation"] else None
            if sql is repo._SET_PRESSURE:
                if params["generation"] != state["generation"]:
                    return None
                state["target"] = max(state["target"] or 0, params["through_id"])
                return state["target"]
            raise AssertionError("pressure must bypass normal cooldown only")
        async def execute(self, sql, params):
            state["queries"].append((sql, params))
            assert sql is repo._RETENTION_PENDING
            return Result([state["active"]] if state["active"] else [])
    async def enabled(*args, **kwargs):
        return state["enabled"]
    async def adopted(*args, **kwargs):
        return state["adopted"]
    async def latest(*args, **kwargs):
        return state.get("previous")
    async def generation(*args):
        return state["generation"]
    async def lock(*args):
        pass
    async def load(*args, **kwargs):
        return [m for m in sources if kwargs["after_id"] < m.id <= kwargs["through_id"]]
    async def enqueue(*args, **kwargs):
        state["queued"].append(kwargs)
        return uuid.uuid4()
    monkeypatch.setattr(context_retention, "enabled_now", enabled)
    monkeypatch.setattr(context_retention, "adopted_user", adopted)
    monkeypatch.setattr(repo, "load_latest", latest)
    monkeypatch.setattr(repo, "read_memory_generation", generation)
    monkeypatch.setattr(repo, "advisory_xact_lock", lock)
    monkeypatch.setattr(repo, "load_range", load)
    monkeypatch.setattr(repo, "enqueue_checkpoint", enqueue)
    monkeypatch.setattr(repo, "ensure_pressure_ingest", AsyncMock(return_value=False))
    monkeypatch.setattr(repo, "wake_checkpoint_ingest", AsyncMock(return_value=False))
    return SimpleNamespace(uid=uid, state=state, sources=sources, session=Session())


async def test_pressure_reserves_small_persisted_turn_immediately_without_normal_target(reservation):
    env = reservation
    result = await repo.request_pressure(env.session, user_id=env.uid, generation=7)
    assert result.status == "queued" and result.through_message_id == 2
    queued = env.state["queued"][0]
    assert queued["delay_s"] == 0 and queued["plan"].source_message_ids == (1, 2)
    assert queued["plan"].reason == "input_pressure"
    assert all(sql is not repo._SAVE_RETENTION_TARGET for sql, _ in env.state["queries"])


async def test_active_normal_or_pressure_job_payload_is_never_changed(reservation):
    env = reservation
    payload = {"reason": "input_pressure", "through_message_id": 1}
    env.state["active"] = (uuid.uuid4(), "ready", 0, None, payload)
    result = await repo.request_pressure(env.session, user_id=env.uid, generation=7)
    assert result.status == "pending" and env.state["target"] == 2
    assert payload == {"reason": "input_pressure", "through_message_id": 1}
    assert not env.state["queued"]


@pytest.mark.parametrize(("field", "value", "status"), [
    ("enabled", False, "disabled"), ("adopted", False, "not_adopted"),
    ("boundary", None, "no_source"), ("generation", 8, "no_source"),
])
async def test_disabled_unadopted_missing_or_old_generation_never_create_target(reservation, field, value, status):
    env = reservation
    env.state[field] = value
    result = await repo.request_pressure(env.session, user_id=env.uid, generation=7)
    assert result.status == status and env.state["target"] is None and not env.state["queued"]


async def test_target_keeps_larger_same_generation_goal(reservation):
    env = reservation
    env.state["target"] = 10
    env.state["active"] = (uuid.uuid4(), "running", 1, None, {})
    result = await repo.request_pressure(env.session, user_id=env.uid, generation=7)
    assert result.through_message_id == 10 and env.state["target"] == 10
    repo.ensure_pressure_ingest.assert_awaited_once_with(env.session, user_id=env.uid,
                                                        generation=7, through_id=10)
    repo.wake_checkpoint_ingest.assert_awaited_once_with(env.session, user_id=env.uid,
                                                      generation=7, through_id=10)


async def test_pressure_source_page_never_marks_final_unanswered_user_complete(monkeypatch):
    sources = [message(i, "moly" if i % 2 == 0 else "user", size=1) for i in range(1, 122)]
    sources[118] = message(119, size=15000)
    async def load(*args, **kwargs):
        return sources[:119]  # deliberately ends before the reply in the fixed larger target
    monkeypatch.setattr(repo, "load_range", load)
    result = await repo.load_pressure_sources(None, uuid.uuid4(), after_id=0, through_id=122)
    assert result[-1].id == 118 and 119 not in [m.id for m in result]


async def test_persisted_pressure_target_bypasses_tail_on_next_sweep(monkeypatch):
    setup = install(monkeypatch, pending=None, pressure=55,
        messages=[message(41, size=10), message(55, "moly", size=10)])
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 1 and result.blocked == 0
    reserve = next(data for name, data in setup.calls if name == "enqueue")
    assert reserve["pressure_through_message_id"] == 55
    assert [m.id for m in reserve["messages"]] == [41, 55]


async def test_pressure_goal_survives_exhausted_job_without_automatic_replay(monkeypatch):
    setup = install(monkeypatch, pending=None, pressure=55, made=False, dead=uuid.uuid4(),
        messages=[message(41, size=10), message(55, "moly", size=10)])
    for _ in range(3):
        result = await sweep._resume_retention_checkpoints()
        assert result.blocked == 1 and result.enqueued == result.replayed == 0
    assert not any(name in {"replay", "unpublished", "resume_enqueue"}
                   for name, _data in setup.calls)
    # No coverage was published and no pressure-clear path was invoked. The
    # persisted goal remains available for an explicit exact-job manual replay.
    criteria = [data for name, data in setup.calls if name == "dead"]
    assert len(criteria) == 3 and all(data["pressure"] for data in criteria)
    assert all(data["through_id"] == 55 for data in criteria)


async def test_old_single_turn_stall_sweep_requests_pressure_without_new_chat(monkeypatch):
    setup = install(monkeypatch, pending=None,
        messages=[message(41, size=5550), message(55, "moly", size=1500)])
    calls = []
    async def request(session, **kwargs):
        calls.append(kwargs)
        return repo.PressureReservation("queued", 55, uuid.uuid4())
    monkeypatch.setattr(repo, "request_pressure", request)
    result = await sweep._resume_retention_checkpoints()
    assert result.enqueued == 1 and calls == [{"user_id": setup.uid, "generation": 7}]


async def test_cursor_cas_has_own_transaction_and_keeps_expected_value(monkeypatch):
    events = []
    uid, old = uuid.uuid4(), str(uuid.uuid4())
    class Session:
        async def __aenter__(self):
            events.append("enter")
            return self
        async def __aexit__(self, *args):
            events.append("exit")
        async def execute(self, sql, params):
            assert sql is sweep._RESUME_CURSOR_WRITE
            assert params == {"expected": json.dumps(old), "next": json.dumps(str(uid))}
            events.append("cas")
            return Result([])  # competing sweep already advanced it
        async def commit(self):
            events.append("commit")
    monkeypatch.setattr(sweep, "get_sessionmaker", lambda: Session)
    assert not await sweep._advance_resume_cursor(expected=old, visited=uid)
    assert events == ["enter", "cas", "commit", "exit"]


async def test_sweep_round_robin_visits_beyond_first_fifty_busy_users(monkeypatch):
    users = [uuid.UUID(int=index) for index in range(1, 76)]
    state = {"cursor": None, "visited": [], "sessions": 0}
    class Session:
        def __init__(self):
            self.user_session = False
        async def __aenter__(self):
            state["sessions"] += 1
            return self
        async def __aexit__(self, *args):
            state["sessions"] -= 1
        async def scalar(self, sql, params=None):
            if sql is sweep._RESUME_CURSOR_READ:
                return state["cursor"]
            assert sql is sweep._TRY_DOCUMENT_USER_LOCK
            self.user_session = True
            state["visited"].append(uuid.UUID(params["user_id"]))
            return False
        async def execute(self, sql, params):
            if sql is sweep._RESUME_CURSOR_WRITE:
                assert state["sessions"] == 1 and not self.user_session
                expected = json.loads(params["expected"])
                if state["cursor"] != expected:
                    return Result([])
                state["cursor"] = json.loads(params["next"])
                return Result([("context_retention_resume_cursor",)])
            cutoff = uuid.UUID(params["cursor"]) if params["cursor"] else None
            if sql is sweep._RETENTION_RESUME_USERS:
                found = [uid for uid in users if cutoff is None or uid > cutoff]
            else:
                assert sql is sweep._RETENTION_RESUME_WRAP
                found = [uid for uid in users if uid <= cutoff]
            return Result([(uid, 0) for uid in found[:params["limit"]]])
        async def commit(self):
            pass
    async def enabled(*args, **kwargs):
        return True
    async def resolve_release(session):
        return None
    monkeypatch.setattr(sweep.retention_release, "resolve", resolve_release)
    monkeypatch.setattr(sweep, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(sweep, "_retention_enabled", enabled)
    first = await sweep._resume_retention_checkpoints()
    second = await sweep._resume_retention_checkpoints()
    assert first.busy == second.busy == 50 and first.blocked == second.blocked == 0
    assert state["visited"][:50] == users[:50]
    assert state["visited"][50:] == users[50:]+users[:25]
    assert len(set(state["visited"])) == 75
    assert state["cursor"] == str(users[24])


def test_pressure_clear_is_generation_and_current_goal_coverage_conditional():
    sql = str(repo._CLEAR_PRESSURE)
    assert "cc.memory_generation=:generation" in sql
    assert "cc.retention_pressure_through_message_id<=:through_id" in sql
    assert "c.through_message_id>=cc.retention_pressure_through_message_id" in sql
    assert "processing_complete" in sql and "c.publish_state='published'" in sql


def test_pressure_reservation_only_uses_complete_committed_turns():
    sql = str(repo._PRESSURE_BOUNDARY)
    assert "m.turn_seq<=cc.last_committed_turn_seq)" in sql
    assert "m.turn_seq<=cc.last_committed_turn_seq+1" in sql
    assert "m.sender='moly' AND m.turn_position=2" in sql
    assert "u.sender='user' AND u.turn_position=1" in sql


def test_input_budget_helper_distinguishes_uncompressible_current_input():
    assert context_retention.fits_input_budget("system", [{"role": "user", "content": "small"}])
    assert not context_retention.fits_input_budget("system", [{"role": "user", "content": "x"*20000}])
