"""Preparation never publishes through the old reader or borrows another release's cursor."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import checkpoint as cp, checkpoint_repo as repo, contract_repo
from app.services import context_retention as cr, retention_release as release
from app.services.retention_preparation_context import Binding, use_binding


@pytest.mark.parametrize("building,state,allowed", [
    (True, "building", True), (True, "prepared", True), (True, "active", False),
    (True, "aborted", False), (False, "active", True), (False, "building", False),
    (False, None, False),
])
async def test_registry_permission_is_exact_and_locked(building, state, allowed):
    value = Binding(uuid.uuid4(), building)
    session = SimpleNamespace(scalar=AsyncMock(return_value=state))
    with use_binding(value):
        if allowed:
            assert await release.require_binding(session, lock=True) == value
        else:
            with pytest.raises(release.ReleaseError):
                await release.require_binding(session, lock=True)
    sql, params = session.scalar.await_args.args
    assert "FOR SHARE" in str(sql) and params == {"preparation_id": value.preparation_id}


async def test_explicit_none_does_not_rebind_an_inflight_old_request(monkeypatch):
    from app.services import config_store
    read = AsyncMock(side_effect=AssertionError("must not read new global identity"))
    monkeypatch.setattr(config_store, "get_config_values", read)
    with use_binding(None):
        assert await release.resolve(object()) is None
    read.assert_not_awaited()


@pytest.mark.parametrize("building,state", [(True, "ready"), (False, "published")])
@pytest.mark.parametrize("statement", [repo._LATEST_RETENTION_SQL, repo._COUNT_SQL,
    repo._INSERT_RETENTION_SQL, repo._RETENTION_COOLDOWN, repo._CLEAR_PRESSURE,
    repo._REMAINING_PRESSURE_GOAL, cr._HISTORICAL_ITEM, cr._PENDING_CORRECTIONS])
def test_all_checkpoint_reads_use_exact_release_and_visibility(building, state, statement):
    value = Binding(uuid.uuid4(), building)
    with use_binding(value):
        sql = str(release.checkpoint_sql(statement))
        parameters = release.params()
    assert "c.preparation_id=CAST(:preparation_id AS uuid)" in sql
    assert "c.preparation_id IS NULL" not in sql
    if building:
        assert "c.publish_state='published'" not in sql and "c.publish_state = 'published'" not in sql
    else:
        assert "c.publish_state='published' OR (c.publish_state='ready'" in sql
        assert "c.validation_json->'activation_sealed'='true'::jsonb" in sql
    assert parameters == {"preparation_id": value.preparation_id, "preparation_state": state,
                          "preparation_building": "true" if building else "false"}


@pytest.mark.parametrize("statement", [repo._RETENTION_PENDING, cr._CONTRACT_ACTIVE,
    cr._CONTRACT_LATEST_TERMINAL, cr._CONTRACT_BASE_JOB, cr._CONTRACT_UNPROCESSED_TERMINAL])
def test_pending_resume_and_dead_work_are_scoped_by_payload_release(statement):
    with use_binding(Binding(uuid.uuid4(), True)):
        sql = str(release.job_sql(statement))
    assert "retention_preparation_id" in sql and "CAST(CAST(:preparation_id AS uuid) AS text)" in sql
    if statement is cr._CONTRACT_UNPROCESSED_TERMINAL:
        assert "active_job.payload->>'retention_preparation_id'" in sql
        assert "failed.payload->>'retention_preparation_id'" in sql


def test_source_hash_none_keeps_old_bytes_and_release_id_separates_same_source():
    import hashlib
    messages = [cp.SourceMessage(7, "user", "normal", "synthetic 🙂")]
    old = cp.source_hash(previous=None, messages=messages)
    assert old == cp.source_hash(previous=None, messages=messages, preparation_id=None)
    first, second = uuid.uuid4(), uuid.uuid4()
    assert len({old, cp.source_hash(previous=None, messages=messages, preparation_id=first),
        cp.source_hash(previous=None, messages=messages, preparation_id=second)}) == 3
    assert len(old) == hashlib.sha256().digest_size*2
    plan = cp.plan(messages, previous=None, version=cp.RETENTION_SUMMARIZER_VERSION,
        reason="input_pressure", through_message_id=7, preparation_id=first)
    assert plan.source_hash == cp.source_hash(previous=None, messages=messages,
        version=cp.RETENTION_SUMMARIZER_VERSION, preparation_id=first)


@pytest.mark.parametrize("name", ["wake_checkpoint_ingest", "ensure_pressure_ingest"])
async def test_private_preparation_never_wakes_or_enqueues_public_ingest(name):
    with use_binding(Binding(uuid.uuid4(), True)):
        assert not await getattr(repo, name)(object(), user_id=uuid.uuid4(), generation=0, through_id=5)


async def test_empty_prepared_contract_is_a_real_progress_receipt_not_a_public_write():
    value = Binding(uuid.uuid4(), True)
    session = SimpleNamespace(scalar=AsyncMock(side_effect=["building", uuid.uuid4()]),
        execute=AsyncMock(return_value=SimpleNamespace(first=lambda: None)))
    with use_binding(value):
        await release.store_contract(session, user_id=uuid.uuid4(), generation=2, locale="en",
            items=[], rendered="", digest="a"*64, through=40,
            source_rows=[(40, "user", "synthetic", None)], after=0)
    sql, params = session.scalar.await_args.args
    assert "INSERT INTO retention_prepared_contracts" in str(sql)
    assert "user_interaction_contract" not in str(sql)
    assert params["document"] == "[]" and params["through"] == 40
    assert params["preparation_state"] == "ready"
    assert all("user_interaction_contract" not in str(call.args[0]) for call in session.execute.await_args_list)


async def test_aborted_preparation_cursor_cannot_certify_another_preparation(monkeypatch):
    session = SimpleNamespace(scalar=AsyncMock(return_value="building"),
        execute=AsyncMock(return_value=SimpleNamespace(first=lambda: None)))
    with use_binding(Binding(uuid.uuid4(), True)):
        assert await release.contract_cursor(session, user_id=uuid.uuid4(), generation=4) == 0
    sql, parameters = session.execute.await_args.args
    assert "preparation_id=:preparation_id" in str(sql)
    assert "chat_contexts" not in str(sql)
    assert parameters["generation"] == 4


async def test_bound_contract_reader_uses_private_document_only(monkeypatch):
    document = AsyncMock(return_value=([], 13, "hash"))
    monkeypatch.setattr(release, "contract_row", document)
    session = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("no legacy query")))
    with use_binding(Binding(uuid.uuid4(), True)):
        assert await contract_repo.retained_document(session, user_id=uuid.uuid4(), generation=0) == []
    document.assert_awaited_once()
    session.execute.assert_not_awaited()


async def test_building_work_is_authorized_while_public_switch_is_off():
    session = SimpleNamespace(scalar=AsyncMock(return_value="building"))
    with use_binding(Binding(uuid.uuid4(), True)):
        assert await cr.enabled_now(session, lock=True)
        assert await cr.adopted_user(session, user_id=uuid.uuid4(), lock=True)
    assert all("retention_preparations" in str(call.args[0]) for call in session.scalar.await_args_list)


@pytest.mark.parametrize("building", [True, False])
async def test_real_contract_worker_stores_only_release_document(contract_environment, monkeypatch, building):
    from tests.test_background_context_budget import job
    from worker import contract_jobs as worker
    state, session = contract_environment
    state["body"] = "This conversation, use short replies."
    stored = []
    async def cursor(*args, **kwargs):
        return 0
    async def store(*args, **kwargs):
        stored.append(kwargs)
        return uuid.uuid4()
    monkeypatch.setattr(release, "contract_cursor", cursor)
    monkeypatch.setattr(release, "store_contract", store)
    public = AsyncMock(side_effect=AssertionError("must never publish old contract"))
    monkeypatch.setattr(contract_repo, "publish", public)
    with use_binding(Binding(uuid.uuid4(), building)):
        result = await worker.handle_contract_compile(job({"retention_enabled": True,
            "memory_generation": 0, "through_message_id": 1}))
        await result.apply_domain(session)
    assert len(stored) == 1 and stored[0]["through"] == 1
    assert stored[0]["items"][0]["scope"] == "conversation"
    assert state["cursor"] == 0 and not state["docs"] and not state["published"]
    public.assert_not_awaited()


async def test_actual_checkpoint_worker_accepts_private_building_while_global_off(environment, monkeypatch):
    from dataclasses import replace
    from tests.test_retention_worker import make_job
    from worker import checkpoint_jobs as worker
    state, session = environment
    private = uuid.uuid4()
    monkeypatch.setattr(worker.config_store, "get_config_values", AsyncMock(
        return_value={"context_retention_v2_enabled": False}))
    original = make_job(state["messages"])
    payload = {**original.payload, "source_hash": cp.source_hash(previous=None,
        messages=state["messages"], version=cp.RETENTION_SUMMARIZER_VERSION, preparation_id=private)}
    with use_binding(Binding(private, True)):
        result = await worker.handle_checkpoint(replace(original, payload=payload))
        await result.apply_domain(session)
    assert state["models"] == 1 and state["enqueued"]
    assert not state["inserts"]  # only durable publish-only child at this point


# Reuse domain fixtures, retaining their real worker implementations.
from tests.test_background_context_budget import contract_environment as contract_environment  # noqa: E402
from tests.test_retention_worker import environment as environment  # noqa: E402


@pytest.mark.parametrize("building", [True, False])
def test_dedup_is_idempotent_and_includes_both_release_and_lane(building):
    value = Binding(uuid.uuid4(), building)
    with use_binding(value):
        result = release.dedup_key("logical")
        assert release.dedup_key(result) == result
    with use_binding(Binding(value.preparation_id, not building)):
        assert release.dedup_key("logical") != result
    assert str(value.preparation_id) in result


@pytest.mark.parametrize("statement", [repo._RETENTION_PENDING, cr._CONTRACT_ACTIVE,
    cr._CONTRACT_UNPROCESSED_TERMINAL])
def test_null_compatibility_jobs_do_not_resume_or_block_preparation(statement):
    with use_binding(None):
        assert "retention_preparation_id') IS NULL" in str(release.job_sql(statement))


async def test_private_checkpoint_insert_is_ready_and_registry_fenced():
    value = Binding(uuid.uuid4(), True)
    session = SimpleNamespace(scalar=AsyncMock(return_value="building"),
        execute=AsyncMock(return_value=SimpleNamespace(first=lambda: (uuid.uuid4(),))))
    with use_binding(value):
        result = await repo.insert(session, user_id=uuid.uuid4(), through_message_id=2,
            summary="", source_hash="a"*64, expected_generation=0,
            version=cp.RETENTION_SUMMARIZER_VERSION,
            state_json=cr.empty_state(), validation_json={"processing_complete": True})
    assert result is not None
    sql, params = session.execute.await_args.args
    assert "preparation_id,publish_state" in str(sql)
    assert params["preparation_id"] == value.preparation_id and params["preparation_state"] == "ready"
    assert "FOR SHARE" in str(session.scalar.await_args.args[0])


async def test_building_plan_does_not_move_public_anchor_or_pending_anchor(monkeypatch):
    captured = []
    class Session:
        async def execute(self, sql, params):
            captured.append(str(sql))
            return SimpleNamespace(first=lambda: None)
        async def scalar(self, sql, params):
            return False
    monkeypatch.setattr(repo, "load_latest", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "read_memory_generation", AsyncMock(return_value=0))
    enqueue = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(repo, "enqueue_checkpoint", enqueue)
    with use_binding(Binding(uuid.uuid4(), True)):
        await repo._maybe_enqueue_retention(Session(), user_id=uuid.uuid4(),
            messages=[cp.SourceMessage(1, "user", "normal", "synthetic" * 150),
                      cp.SourceMessage(2, "moly", "normal", "response")],
            keep_from_message_id=2, reset_triggered=True)
    enqueue.assert_awaited_once()
    assert all("UPDATE chat_contexts" not in sql for sql in captured)


async def test_active_release_with_writer_off_stays_readable_but_cannot_write(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "context_retention_v2_enabled", False)
    class Session:
        async def scalar(self, sql, params):
            assert "retention_preparations" in str(sql)
            return "active"
        async def execute(self, sql):
            assert sql is cr._FLAG_LOCK
            return SimpleNamespace(first=lambda: (False,))
    with use_binding(Binding(uuid.uuid4(), False)):
        assert await cr.adopted_user(Session(), user_id=uuid.uuid4(), lock=True)
        assert not await cr.enabled_now(Session(), lock=True)


async def test_new_public_fact_child_can_capture_active_release_without_rebinding_parent(monkeypatch):
    from app.services.retention_preparation_context import current_binding
    value = Binding(uuid.uuid4(), False)
    session = SimpleNamespace(scalar=AsyncMock(return_value="active"))
    with use_binding(None):
        assert await release.resolve(session, config={"context_retention_active_preparation": str(value.preparation_id)}) == value
        assert current_binding() is None
    assert "retention_preparations" in str(session.scalar.await_args.args[0])


async def test_explicit_old_config_without_active_identity_does_not_query_again():
    session = SimpleNamespace(scalar=AsyncMock(side_effect=AssertionError("unexpected query")))
    with use_binding(None):
        assert await release.resolve(session, config={}) is None
    session.scalar.assert_not_awaited()


async def test_high_impact_drafts_are_retained_without_activation_or_legacy_writes():
    session = SimpleNamespace(scalar=AsyncMock(side_effect=["building", uuid.uuid4(), uuid.uuid4()]),
        execute=AsyncMock(return_value=SimpleNamespace(first=lambda: None)))
    with use_binding(Binding(uuid.uuid4(), True)):
        await release.store_contract(session, user_id=uuid.uuid4(), generation=0, locale="en",
            items=[], rendered="", digest="a"*64, through=4,
            source_rows=[(4, "user", "synthetic", None)], after=0, draft_items=[{"generation": 0, "status": "active", "synthetic": "draft"}])
    writes = [call.args for call in session.scalar.await_args_list if "INSERT INTO" in str(call.args[0])]
    assert [values["preparation_state"] for _, values in writes] == ["ready", "draft"]
    assert all("user_interaction_contract" not in str(sql) for sql, _ in writes)
    assert "status" not in str(writes[-1][0]).split("WHERE preparation_id=:preparation_id")[-1]


async def test_public_fact_child_captures_active_only_inside_new_reservation(monkeypatch):
    from app.services import jobs
    from app.services.retention_preparation_context import current_binding
    value = Binding(uuid.uuid4(), False)
    monkeypatch.setattr(cr, "capture_privacy_epoch", AsyncMock(return_value=0))
    monkeypatch.setattr(cr, "privacy_epoch_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(release, "contract_cursor", AsyncMock(return_value=0))
    captured = []
    async def enqueue(*args, **kwargs):
        captured.append((current_binding(), kwargs))
        return uuid.uuid4()
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    class Session:
        async def scalar(self, sql, params):
            return "active" if "retention_preparations" in str(sql) else None
        async def execute(self, sql, params):
            return SimpleNamespace(first=lambda: None)
    with use_binding(None):
        await cr.enqueue_contract_processing(Session(), user_id=uuid.uuid4(), generation=0,
            through_id=5, config={"context_retention_active_preparation": str(value.preparation_id)})
        assert current_binding() is None
    assert captured[0][0] == value
    assert captured[0][1]["dedup_key"].endswith(f":preparation:{value.preparation_id}:0")


async def test_off_compatibility_is_global_even_for_an_unbound_fact_job_without_v5():
    session = SimpleNamespace(scalar=AsyncMock(return_value="active"))
    with use_binding(None):
        assert await cr.adopted_user(session, user_id=uuid.uuid4(), config={
            "context_retention_v2_enabled": False,
            "context_retention_active_preparation": str(uuid.uuid4()),
        })
    assert all("retention_preparations" in str(call.args[0]) for call in session.scalar.await_args_list)
