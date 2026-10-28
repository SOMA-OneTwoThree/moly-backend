"""Shared diary routing modes and source-bound worker writes."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import diary_recall_repo, diary_shared_embeddings as shared


class _Result:
    def __init__(self, row=None):
        self.row = row

    def first(self):
        return self.row


class _Session:
    def __init__(self, attached=None):
        self.attached = attached
        self.executed = []

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        return _Result(self.attached if stmt is shared._ATTACH else None)


@pytest.mark.parametrize("mode", ["private", "dual", "shared"])
async def test_storage_mode_uses_only_configured_allowed_value(monkeypatch, mode):
    read = AsyncMock(return_value={shared.MODE_KEY: mode})
    monkeypatch.setattr(shared.config_store, "get_config_values", read)
    session = object()
    assert await shared.storage_mode(session) == mode
    read.assert_awaited_once_with(session, [shared.MODE_KEY])


async def test_missing_storage_mode_preserves_private_default(monkeypatch):
    monkeypatch.setattr(shared.config_store, "get_config_values", AsyncMock(return_value={}))
    assert await shared.storage_mode(object()) == "private"


@pytest.mark.parametrize("mode", [None, True, False, "", "DUAL", "duel", {}, ["private"], 0])
async def test_invalid_storage_mode_fails_instead_of_enabling_shared_writes(monkeypatch, mode):
    monkeypatch.setattr(
        shared.config_store, "get_config_values", AsyncMock(return_value={shared.MODE_KEY: mode}),
    )
    with pytest.raises(ValueError, match="invalid diary embedding storage mode"):
        await shared.storage_mode(object())


@pytest.fixture
def routes(monkeypatch):
    state = SimpleNamespace(mode="private", row=None, events=[])

    async def lock(session, user_id):
        state.events.append(("lock", user_id))

    async def mode(session):
        state.events.append(("mode",))
        return state.mode

    async def owned(session, user_id, diary_id, *, lock=False):
        state.events.append(("owned", user_id, diary_id, lock))
        return state.row

    monkeypatch.setattr(shared, "advisory_xact_lock", lock)
    monkeypatch.setattr(shared, "storage_mode", mode)
    monkeypatch.setattr(shared, "_owned", owned)
    state.enqueue = AsyncMock()
    monkeypatch.setattr(shared, "enqueue_shared", state.enqueue)
    state.embed = AsyncMock(side_effect=AssertionError("routing must not call the provider"))
    monkeypatch.setattr(diary_recall_repo, "embed_text", state.embed)
    return state


def _row(**overrides):
    return {
        "shared_embedding_id": None,
        "valid_shared_id": None,
        "shared_ready": False,
        "approved": True,
        "private_ready": False,
        **overrides,
    }


@pytest.mark.parametrize("mode", ["private", "dual", "shared"])
async def test_deleted_or_changed_owned_projection_performs_no_shared_write(routes, mode):
    routes.mode = mode
    session = _Session()
    uid, did = uuid.uuid4(), uuid.uuid4()
    assert await shared.ensure_for_diary(session, user_id=uid, diary_id=did) is False
    assert session.executed == []
    assert routes.events == [("lock", uid), ("mode",), ("owned", uid, did, True)]
    routes.enqueue.assert_not_awaited()
    routes.embed.assert_not_awaited()


@pytest.mark.parametrize("mode,approved", [("private", True), ("private", False), ("dual", False), ("shared", False)])
async def test_private_mode_or_unapproved_copy_keeps_the_private_path(routes, mode, approved):
    routes.mode = mode
    routes.row = _row(approved=approved)
    session = _Session()
    assert await shared.ensure_for_diary(session, user_id=uuid.uuid4(), diary_id=uuid.uuid4()) is False
    assert session.executed == []
    routes.enqueue.assert_not_awaited()


async def test_invalid_pointer_is_detached_before_private_fallback(routes):
    routes.row = _row(shared_embedding_id=uuid.uuid4(), approved=False)
    session = _Session()
    uid, did = uuid.uuid4(), uuid.uuid4()
    assert await shared.ensure_for_diary(session, user_id=uid, diary_id=did) is False
    assert len(session.executed) == 1
    assert session.executed[0][1] == {"user_id": uid, "diary_id": did}
    assert "shared_embedding_id=NULL" in str(session.executed[0][0])
    routes.enqueue.assert_not_awaited()


@pytest.mark.parametrize("mode,materialize", [("private", True), ("dual", True), ("shared", False)])
async def test_existing_ready_canonical_is_reused_without_creating_or_enqueuing(routes, mode, materialize):
    sid = uuid.uuid4()
    routes.mode = mode
    # Historical approved copy can have changed since its canonical was selected.
    routes.row = _row(shared_embedding_id=sid, valid_shared_id=sid, shared_ready=True, approved=False)
    session = _Session(attached=(sid, True))
    assert await shared.ensure_for_diary(session, user_id=uuid.uuid4(), diary_id=uuid.uuid4()) is True
    assert [s for s, _params in session.executed] == [shared._SEED, shared._ATTACH]
    assert session.executed[-1][1]["materialize"] is materialize
    routes.enqueue.assert_not_awaited()
    routes.embed.assert_not_awaited()


@pytest.mark.parametrize("mode,routed", [("dual", False), ("shared", True)])
async def test_new_pending_shared_source_is_scheduled_and_dual_keeps_private_work(routes, mode, routed):
    sid = uuid.uuid4()
    routes.mode = mode
    routes.row = _row()
    session = _Session(attached=(sid, False))
    assert await shared.ensure_for_diary(session, user_id=uuid.uuid4(), diary_id=uuid.uuid4()) is routed
    assert [s for s, _params in session.executed] == [shared._INSERT, shared._SEED, shared._ATTACH]
    routes.enqueue.assert_awaited_once_with(session, sid)
    routes.embed.assert_not_awaited()


@pytest.mark.parametrize("mode", ["dual", "shared"])
async def test_different_historical_private_vector_is_preserved(routes, mode):
    routes.mode = mode
    routes.row = _row(private_ready=True)
    session = _Session(attached=None)
    assert await shared.ensure_for_diary(session, user_id=uuid.uuid4(), diary_id=uuid.uuid4()) is True
    # No provider call or canonical replacement is made. The failed attachment
    # only detaches a pointer on an existing non-NULL private vector.
    assert [s for s, _ in session.executed[:3]] == [shared._INSERT, shared._SEED, shared._ATTACH]
    assert "AND embedding IS NOT NULL" in str(session.executed[-1][0])
    routes.enqueue.assert_not_awaited()
    routes.embed.assert_not_awaited()


@pytest.mark.parametrize(
    "mode,valid,ready,approved,expected",
    [
        ("private", False, False, True, False),
        ("dual", False, False, True, False),
        ("shared", False, False, True, True),
        ("shared", False, False, False, False),
        ("private", True, True, False, True),
        ("private", True, False, False, True),
        ("dual", True, True, False, True),
        ("dual", True, False, False, False),
        ("shared", True, False, False, True),
    ],
)
async def test_worker_route_decision_keeps_existing_references_readable(routes, mode, valid, ready, approved, expected):
    routes.mode = mode
    routes.row = _row(valid_shared_id=uuid.uuid4() if valid else None, shared_ready=ready, approved=approved)
    assert await shared.should_route(object(), user_id=uuid.uuid4(), diary_id=uuid.uuid4()) is expected


async def test_shared_job_does_not_inherit_a_user_or_raw_copy(monkeypatch):
    enqueue = AsyncMock()
    monkeypatch.setattr(shared.jobs, "enqueue", enqueue)
    sid, session = uuid.uuid4(), object()
    await shared.enqueue_shared(session, sid)
    args = enqueue.await_args
    assert args.args == (session,)
    assert args.kwargs["user_id"] is None
    assert args.kwargs["payload"] == {"schema_version": shared.INDEX_VERSION, "shared_id": str(sid)}
    assert args.kwargs["dedup_key"] == f"diary-template:{sid}"


def _source():
    return {
        "id": uuid.uuid4(), "search_text": "approved copy", "source_hash": "h",
        "embedding_model": "model", "index_version": shared.INDEX_VERSION, "dimensions": shared.DIMENSIONS,
    }


async def test_shared_write_carries_loaded_source_identity_and_handles_a_lost_cas():
    session, source = _Session(), _source()
    # Zero returned rows means it became ready/changed while the provider ran.
    assert await shared.write_shared(session, source=source, vector=[0.0] * shared.DIMENSIONS) is False
    assert len(session.executed) == 1
    parameters = session.executed[0][1]
    assert {k: v for k, v in parameters.items() if k != "vector"} == source
    assert len(parameters["vector"].strip("[]").split(",")) == shared.DIMENSIONS


async def test_wrong_dimension_shared_vector_never_touches_the_database():
    session = _Session()
    with pytest.raises(ValueError, match="dimensions"):
        await shared.write_shared(session, source=_source(), vector=[0.0])
    assert session.executed == []


async def test_unattachable_pending_canonical_is_an_error_not_a_successful_noop(routes):
    routes.mode = "shared"
    routes.row = _row(embedding=None)
    session = _Session(attached=None)
    with pytest.raises(ValueError, match="input mismatch"):
        await shared.ensure_for_diary(session, user_id=uuid.uuid4(), diary_id=uuid.uuid4())
    routes.enqueue.assert_not_awaited()
    routes.embed.assert_not_awaited()
