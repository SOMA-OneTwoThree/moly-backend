"""Release preparation is whole-population, source-bound and offline-testable."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import checkpoint, checkpoint_repo, context_retention, contract_repo, memory
from app.services.mem0_ingest import candidate_hash
from scripts import prepare_guard_context_release as tool

DEV = "wywzjslvxwttxkecbyis"
PROD = tool.PROD_PROJECT_REF  # Identifier only; all tests are offline.


def dsn(ref=DEV):
    return f"postgresql://postgres.{ref}:synthetic@aws-0-region.pooler.supabase.com:5432/postgres"


def fixture():
    uid, rid, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    raw = "prefix verified source suffix"
    quote = "verified source"
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    messages = [dict(id=1, sender="user", kind="normal", content=raw, created_at=now,
                     turn_seq=1, turn_position=1),
                dict(id=2, sender="moly", kind="normal", content="reply", created_at=now,
                     turn_seq=1, turn_position=2)]
    user = dict(user_id=uid, language="en", timezone="UTC", memory_generation=0,
                last_committed_turn_seq=1, barrier_state="active", barrier_epoch=0,
                privacy_epoch=0, mode="v2", bootstrap_status="ready", repair_generation=0)
    row = dict(id=rid, provider="mem0", collection_version="v2", provider_memory_id=pid,
               content_hash=candidate_hash(quote), canonical_text=None, candidates=[quote],
               vector_owner=str(uid), vector_data=None, vector_text=None)
    start = memory.sanitize_text(raw).encode().index(quote.encode())
    source = dict(registry_id=rid, source_message_id=1, source_sender="user",
        authority="explicit_user", start_utf8=start, end_utf8=start+len(quote.encode()),
        span_hash=hashlib.sha256(quote.encode()).hexdigest(), source_occurred_at=now,
        actual_message_id=1, message_sender="user", message_kind="normal", message_content=raw)
    return user, messages, row, source


class Result:
    def __init__(self, rows):
        self.rows = rows
    def mappings(self):
        return self
    def all(self):
        return copy.deepcopy(self.rows)
    def first(self):
        return copy.deepcopy(self.rows[0]) if self.rows else None


class Session:
    def __init__(self, user, messages, memories, sources):
        self.user, self.messages, self.memories, self.sources = user, messages, memories, sources
        self.queries = []
    async def execute(self, sql, params=None):
        query = str(sql)
        self.queries.append((query, params))
        if "SELECT p.id AS user_id" in query:
            return Result([self.user])
        if "FROM messages WHERE user_id" in query:
            return Result(self.messages)
        if "ARRAY(SELECT DISTINCT" in query:
            return Result(self.memories)
        if "LEFT JOIN messages m" in query:
            return Result(self.sources)
        if sql is tool._CAS:
            row = next(r for r in self.memories if r["id"] == params["id"])
            assert row["canonical_text"] is None
            row["canonical_text"] = params["canonical"]
            return Result([(row["id"],)])
        return Result([])


def test_target_is_explicit_and_endpoint_bound():
    tool.require_target("dev", dsn(), DEV)
    tool.require_target("prod", dsn(PROD), PROD, True)
    for env, value, ref, ack in [
        ("prod", dsn(PROD), PROD, False), ("dev", dsn(PROD), PROD, False),
        ("prod", dsn(), DEV, True), ("dev", dsn()+"?host=other", DEV, False),
        ("dev", dsn().replace("pooler.supabase.com", "pooler.supabase.com.evil"), DEV, False),
        ("dev", dsn().replace("/postgres", "/other"), DEV, False),
        ("dev", dsn(), "", False),
        ("prod", dsn("a"*20), "a"*20, True),
    ]:
        with pytest.raises(tool.Blocked):
            tool.require_target(env, value, ref, ack)


@pytest.mark.parametrize("origin", ["candidate", "data", "text"])
def test_canonical_requires_same_provider_identity_and_content_hash(origin):
    user, _, row, _ = fixture()
    value = row["candidates"][0]
    row["candidates"] = []
    if origin == "candidate":
        row["candidates"] = [value]
    else:
        row["vector_"+origin] = value
    assert tool.certified_canonical(row, user["user_id"]) == value
    row["content_hash"] = "wrong"
    with pytest.raises(tool.Blocked):
        tool.certified_canonical(row, user["user_id"])


def test_other_owners_provider_text_and_existing_conflicts_are_not_promoted():
    user, _, row, _ = fixture()
    row.update(candidates=[], vector_text="verified source", vector_owner=str(uuid.uuid4()))
    with pytest.raises(tool.Blocked):
        tool.certified_canonical(row, user["user_id"])
    row.update(candidates=["verified source"], canonical_text="invented replacement")
    with pytest.raises(tool.Blocked):
        tool.certified_canonical(row, user["user_id"])


async def test_plan_fingerprint_survives_exact_backfill_but_not_raw_or_epoch_change():
    user, messages, row, source = fixture()
    session = Session(user, messages, [row], [source])
    first = await tool.read_user(session, user)
    row["canonical_text"] = first.canonical[row["id"]]
    assert (await tool.read_user(session, user)).fingerprint == first.fingerprint
    messages[-1]["content"] += " changed"
    assert (await tool.read_user(session, user)).fingerprint != first.fingerprint
    messages[-1]["content"] = "reply"
    user.update(barrier_epoch=1, privacy_epoch=1)
    assert (await tool.read_user(session, user)).fingerprint != first.fingerprint


@pytest.mark.parametrize("change", ["source", "sender", "kind", "authority", "pipeline"])
async def test_bad_source_or_legacy_history_blocks_whole_user(change):
    user, messages, row, source = fixture()
    if change == "source":
        source["message_content"] = "different"
    elif change == "sender":
        source["message_sender"] = "moly"
    elif change == "kind":
        source["message_kind"] = "safety"
    elif change == "authority":
        source["authority"] = "inferred"
    else:
        user["mode"] = "legacy"
    with pytest.raises(ValueError):
        await tool.read_user(Session(user, messages, [row], [source]), user)


async def test_artifact_has_no_accounts_or_source_text_and_allows_partial_resume():
    user, messages, row, source = fixture()
    item = await tool.read_user(Session(user, messages, [row], [source]), user)
    plan = tool.artifact([item], {}, 1, project_ref=DEV)
    encoded = json.dumps(plan)
    assert str(user["user_id"]) not in encoded and source["message_content"] not in encoded
    current = {**plan, "missing_canonical": 0}
    tool.approved_plan(plan, current, tool.digest(plan))
    with pytest.raises(tool.Blocked):
        tool.approved_plan(plan, {**current, "source_fingerprint": "changed"}, tool.digest(plan))
    with pytest.raises(tool.Blocked):
        tool.approved_plan(plan, current, "wrong checksum")
    with pytest.raises(tool.Blocked):
        tool.approved_plan(plan, {**current, "population": 2}, tool.digest(plan))


async def test_prepare_only_cas_and_domain_reservation_no_commit_or_cursor_edit(monkeypatch):
    user, messages, row, source = fixture()
    session = Session(user, messages, [row], [source])
    expected = await tool.read_user(session, user)
    from app.core import advisory_lock
    monkeypatch.setattr(advisory_lock, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(context_retention, "enabled_now", AsyncMock(return_value=True))
    monkeypatch.setattr(context_retention, "privacy_epoch_matches", AsyncMock(return_value=True))
    contract = AsyncMock()
    reserve = AsyncMock(return_value=checkpoint_repo.PressureReservation("queued", 2))
    monkeypatch.setattr(context_retention, "enqueue_contract_processing", contract)
    monkeypatch.setattr(checkpoint_repo, "request_pressure", reserve)
    await tool.prepare_user(session, expected)
    assert row["canonical_text"] == "verified source"
    assert contract.await_args.kwargs["through_id"] == reserve.await_args.kwargs["through_id"] == 2
    assert not any("UPDATE messages" in q or "UPDATE memory_pipeline_states" in q for q, _ in session.queries)
    assert "FOR SHARE" in session.queries[-4][0] or any("FOR SHARE" in q for q, _ in session.queries)


def test_output_reserved_before_writes_and_never_overwrites_or_follows_symlinks(tmp_path):
    output = tmp_path / "plan.json"
    with tool.reserve_private(output) as stream:
        tool.write_private(stream, {"aggregate": 1})
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        tool.reserve_private(output)
    linked = tmp_path / "linked"
    linked.symlink_to(output)
    with pytest.raises(tool.Blocked):
        tool.reserve_private(linked)


async def test_no_schema_or_no_maintenance_guards_precede_any_engine(monkeypatch, tmp_path):
    from db import envfile
    import sqlalchemy.ext.asyncio
    monkeypatch.setattr(envfile, "load_conn", lambda _: dsn())
    engine = AsyncMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", engine)
    args = SimpleNamespace(env="dev", expected_project_ref=DEV, production_ack=False,
        action="prepare", maintenance_ack=None, plan=None, plan_sha256=None,
        output=tmp_path/"unused.json")
    with pytest.raises(tool.Blocked):
        await tool.run(args)
    assert not args.output.exists()
    engine.assert_not_called()


async def test_schema_preflight_reports_blocker_without_legacy_column_queries():
    session = SimpleNamespace(execute=AsyncMock(return_value=Result([])))
    with pytest.raises(tool.Blocked, match="reviewed_migration_required"):
        await tool.collect(session)
    assert session.execute.await_count == 1


@pytest.mark.parametrize("receipt", [True, False])
async def test_audit_recomputes_raw_chain_hash_not_just_numeric_cursors(monkeypatch, receipt):
    user, messages, row, source = fixture()
    row["canonical_text"] = "verified source"
    session = Session(user, messages, [row], [source])
    item = await tool.read_user(session, user)
    sources = [checkpoint.SourceMessage(m["id"], m["sender"], m["kind"], m["content"], m["created_at"]) for m in messages]
    cid = uuid.uuid4()
    record = dict(id=cid, through_message_id=2, previous_checkpoint_id=None, summary="summary",
        version=checkpoint.RETENTION_SUMMARIZER_VERSION, state_json={},
        validation_json={"processing_complete": True, "source_message_ids": [1, 2],
                         "completed_proof": {"synthetic": "validated by domain helper"}},
        coverage_from_message_id=1, coverage_through_message_id=2, segment_through_message_id=2,
        source_hash=checkpoint.source_hash(previous=None, messages=sources, version=checkpoint.RETENTION_SUMMARIZER_VERSION))
    value = checkpoint.Checkpoint(cid, 2, "summary", record["version"], record["source_hash"])
    monkeypatch.setattr(checkpoint_repo, "load_latest", AsyncMock(return_value=value))
    monkeypatch.setattr(checkpoint_repo, "coverage_for_checkpoint", AsyncMock(return_value=SimpleNamespace(through_message_id=2)))
    monkeypatch.setattr(context_retention, "load_required_facts", AsyncMock())
    monkeypatch.setattr(context_retention, "validate_progress_sources", AsyncMock())
    monkeypatch.setattr(checkpoint, "validate_completed_progress", lambda **kwargs: "summary")
    monkeypatch.setattr(contract_repo, "published_text", AsyncMock())
    async def audit_rows(sql, params):
        if "FROM async_jobs" in str(sql):
            return Result([({"after_message_id": 0}, {"through": 2})] if receipt else [])
        return Result([record])
    session.execute = AsyncMock(side_effect=audit_rows)
    assert await tool.audit_user(session, item) is receipt
    record["source_hash"] = "forged despite complete cursor"
    assert not await tool.audit_user(session, item)


async def test_nonzero_generation_never_reopens_unproved_old_raw():
    user, messages, row, source = fixture()
    user["memory_generation"] = 1
    with pytest.raises(tool.Blocked, match="generation_scope_unproved"):
        await tool.read_user(Session(user, messages, [row], [source]), user)


def test_schema_preflight_uses_real_columns_not_orm_computed_properties():
    import re
    schema = (Path(tool.__file__).resolve().parents[1]/"db/schema.sql").read_text()
    for table, column in tool.REQUIRED_COLUMNS:
        body = schema.split(f"CREATE TABLE public.{table} (", 1)[1].split("\n);", 1)[0]
        assert re.search(rf"^\s*{re.escape(column)}\s+", body, re.MULTILINE), (table, column)


async def test_environment_override_must_be_private_before_connection(monkeypatch, tmp_path):
    args = SimpleNamespace(env="dev", env_file=tmp_path/"not-private.env")
    args.env_file.write_text("SUPABASE_DB_CONNECTION_STRING=synthetic")
    args.env_file.chmod(0o644)
    with pytest.raises(tool.Blocked, match="private_environment_file_required"):
        await tool.run(args)


def test_reverify_uses_its_actual_page_manifests_and_preserves_parent_items():
    user, messages, _, _ = fixture()
    state = context_retention.validate_state(None)
    parent = dict(through_message_id=2, source_hash="old-parent", state_json=copy.deepcopy(state))
    state["sequence"] += 1
    source = [checkpoint.SourceMessage(m["id"], m["sender"], m["kind"], m["content"], m["created_at"])
              for m in messages]
    manifests = [{"after": 0, "through": 2, "hash": checkpoint.source_hash(previous=None,
        messages=source, version=checkpoint.RETENTION_SUMMARIZER_VERSION)}]
    current = dict(through_message_id=2, coverage_from_message_id=1, coverage_through_message_id=2,
        segment_through_message_id=2, version=checkpoint.RETENTION_SUMMARIZER_VERSION,
        state_json=state, validation_json={"page_manifests": manifests},
        summary=context_retention.render_state(state), source_hash=hashlib.sha256(json.dumps({
            "parent": parent["source_hash"], "manifests": manifests, "state": state},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest())
    assert tool.reverified_matches(current, parent, messages)
    for mutation in ("raw", "page_gap", "state", "parent"):
        changed, old, raw = copy.deepcopy(current), copy.deepcopy(parent), copy.deepcopy(messages)
        if mutation == "raw":
            raw[0]["content"] += " changed"
        elif mutation == "page_gap":
            changed["validation_json"]["page_manifests"][0]["after"] = 1
        elif mutation == "state":
            changed["state_json"]["sequence"] = 0
        else:
            old["source_hash"] = "other parent"
        assert not tool.reverified_matches(changed, old, raw)


async def test_prepare_never_fills_from_a_changed_plan_or_disabled_global_worker(monkeypatch):
    from app.core import advisory_lock
    user, messages, row, source = fixture()
    session = Session(user, messages, [row], [source])
    expected = await tool.read_user(session, user)
    monkeypatch.setattr(advisory_lock, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(context_retention, "enabled_now", AsyncMock(return_value=False))
    with pytest.raises(tool.Blocked, match="global_worker_or_privacy_not_ready"):
        await tool.prepare_user(session, expected)
    assert row["canonical_text"] is None
    monkeypatch.setattr(context_retention, "enabled_now", AsyncMock(return_value=True))
    monkeypatch.setattr(context_retention, "privacy_epoch_matches", AsyncMock(return_value=True))
    messages[-1]["content"] += " new body"
    with pytest.raises(tool.Blocked, match="plan_source_changed"):
        await tool.prepare_user(session, expected)
    assert row["canonical_text"] is None


@pytest.mark.parametrize("status", ["deleting", "deleted"])
async def test_ineligible_subject_is_rejected_before_any_body_or_vector_query(status):
    user, messages, row, source = fixture()
    user["barrier_state"] = status
    session = Session(user, messages, [row], [source])
    with pytest.raises(tool.Blocked, match="subject_not_authorized"):
        await tool.read_user(session, user)
    assert session.queries == []


async def test_missing_barrier_reuses_compat_but_enforced_blocks_before_body(monkeypatch):
    from app.config import settings
    user, messages, row, source = fixture()
    user.update(barrier_state="missing", barrier_epoch=0, privacy_epoch=0)
    session = Session(user, messages, [row], [source])
    monkeypatch.setattr(settings, "privacy_barrier_mode", "compat")
    assert (await tool.read_user(session, user)).through == 2
    session.queries.clear()
    monkeypatch.setattr(settings, "privacy_barrier_mode", "enforced")
    with pytest.raises(tool.Blocked, match="historical_privacy_not_ready"):
        await tool.read_user(session, user)
    assert session.queries == []


async def test_epoch_mismatch_is_blocked_before_source_query():
    user, messages, row, source = fixture()
    user["barrier_epoch"] = 1
    session = Session(user, messages, [row], [source])
    with pytest.raises(tool.Blocked, match="historical_privacy_not_ready"):
        await tool.read_user(session, user)
    assert session.queries == []


async def test_unanswered_normal_tail_is_not_hidden_by_an_older_complete_boundary():
    user, messages, row, source = fixture()
    messages.append({**messages[0], "id": 3, "turn_seq": 2})
    with pytest.raises(tool.Blocked, match="incomplete_normal_turn"):
        await tool.read_user(Session(user, messages, [row], [source]), user)


async def test_existing_authorization_exclusions_are_counted_and_bound_without_body(monkeypatch):
    user, messages, row, source = fixture()
    plan = await tool.read_user(Session(user, messages, [row], [source]), user)
    excluded = {**user, "user_id": uuid.uuid4(), "barrier_state": "deleting"}
    session = SimpleNamespace(execute=AsyncMock(return_value=Result([user, excluded])))
    monkeypatch.setattr(tool, "schema_ready", AsyncMock(return_value=True))
    body = AsyncMock(return_value=plan)
    monkeypatch.setattr(tool, "read_user", body)
    plans, blockers, population, exclusions = await tool.collect(session)
    body.assert_awaited_once_with(session, user)
    output = tool.artifact(plans, blockers, population, project_ref=DEV, excluded=exclusions)
    assert output["eligible_users"] == 1 and output["population"] == 2
    assert output["excluded_authorization"] == {"deleting": 1}
    assert str(excluded["user_id"]) not in json.dumps(output)
    changed = tool.artifact(plans, blockers, population, project_ref=DEV,
                            excluded=[[str(excluded["user_id"]), "deleted", 0, 0]])
    with pytest.raises(tool.Blocked):
        tool.approved_plan(output, changed, tool.digest(output))


def test_no_rollout_lists_ddl_or_history_mutation_surface():
    source = Path(tool.__file__).read_text()
    for forbidden in ("UPDATE messages", "UPDATE memory_pipeline_states", "replay_dead(",
                      "ALTER TABLE", "GRANT ", "crisis_guard_ready_users"):
        assert forbidden not in source
    assert "c.status='committed'" in tool._MEMORIES
