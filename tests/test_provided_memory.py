"""Only provided optional memories bind a reply; their semantic evidence remains strict."""

import copy
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
import uuid

import pytest

from app.services import mem0_ingest, memory, provided_memory as provided


def source(registry_id, *, message_id=1, content="prefix likes tea suffix", quote="likes tea"):
    data = memory.sanitize_text(content).encode()
    start = data.index(quote.encode())
    return dict(
        registry_id=registry_id,
        source_message_id=message_id,
        source_sender="user",
        authority="explicit_user",
        start_utf8=start,
        end_utf8=start + len(quote.encode()),
        span_hash=hashlib.sha256(quote.encode()).hexdigest(),
        actual_message_id=message_id,
        message_sender="user",
        message_kind="normal",
        message_content=content,
        source_occurred_at=datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
    )


@pytest.fixture
def env():
    owner, rid, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = dict(
        registry_id=rid,
        provider_memory_id=pid,
        provider="mem0",
        collection_version="v2",
        canonical_text="likes tea",
        content_hash=mem0_ingest.candidate_hash("likes tea"),
        occurred_at=datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
        semantic_status="active",
        provider_delete_state="kept",
        duplicate_of_registry_id=None,
        superseded_by_registry_id=None,
        revision=1,
        classification_version="first",
        context_priority="related",
    )
    state = dict(
        owner=owner,
        generation=2,
        mode="v2",
        ready=True,
        rows=[row],
        sources=[source(rid)],
        calls=[],
    )

    class Session:
        async def execute(self, sql, params):
            state["calls"].append((str(sql), copy.deepcopy(params)))
            assert params["user_id"] == owner
            wanted = set(params["ids"])
            if sql is provided._ROWS:
                rows = (
                    [
                        r
                        for r in state["rows"]
                        if r["registry_id"] in wanted
                        and r["collection_version"] == "v2"
                        and r["semantic_status"] == "active"
                        and r["provider_delete_state"] == "kept"
                        and r["duplicate_of_registry_id"] is None
                        and r["superseded_by_registry_id"] is None
                    ]
                    if (
                        params["generation"] == state["generation"]
                        and state["mode"] == "v2"
                        and state["ready"]
                    )
                    else []
                )
            else:
                assert sql is provided._SOURCES
                rows = [r for r in state["sources"] if r["registry_id"] in wanted]
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(all=lambda: copy.deepcopy(rows))
            )

    return state, Session()


async def capture(env, **kwargs):
    state, session = env
    return await provided.capture(
        session,
        user_id=state["owner"],
        generation=2,
        ids=[state["rows"][0]["registry_id"]],
        language="ko",
        today=date(2026, 11, 4),
        **kwargs,
    )


async def valid(env, snapshots):
    state, session = env
    return await provided.validate(
        session, user_id=state["owner"], generation=2, snapshots=snapshots
    )


async def test_revision_classification_priority_and_unrelated_new_required_do_not_invalidate(env):
    state, session = env
    snapshots = await capture(env)
    state["rows"][0].update(revision=400, classification_version="new", context_priority="required")
    unrelated = {**state["rows"][0], "registry_id": uuid.uuid4(), "canonical_text": "unrelated"}
    state["rows"].extend(
        copy.deepcopy({**unrelated, "registry_id": uuid.uuid4()}) for _ in range(300)
    )
    assert await valid(env, snapshots)
    assert all(set(params["ids"]) == {snapshots[0].registry_id} for _, params in state["calls"])
    assert "LIMIT" not in str(provided._ROWS) and "context_priority" not in str(provided._ROWS)


@pytest.mark.parametrize(
    "field,value",
    [
        ("semantic_status", "duplicate"),
        ("semantic_status", "superseded"),
        ("semantic_status", "ambiguous"),
        ("provider_delete_state", "pending"),
        ("collection_version", "v3"),
        ("provider", "other"),
        ("provider_memory_id", uuid.UUID(int=33)),
        ("canonical_text", "likes  tea"),
        ("content_hash", "changed"),
        ("duplicate_of_registry_id", uuid.UUID(int=31)),
        ("superseded_by_registry_id", uuid.UUID(int=32)),
    ],
)
async def test_used_registry_semantics_and_exact_text_cannot_change(env, field, value):
    state, session = env
    snapshots = await capture(env)
    state["rows"][0][field] = value
    assert not await valid(env, snapshots)


async def test_a_different_registry_with_same_body_cannot_replace_original(env):
    state, session = env
    snapshots = await capture(env)
    state["rows"][0]["registry_id"] = uuid.uuid4()
    assert not await valid(env, snapshots)


@pytest.mark.parametrize(
    "field,value",
    [
        ("actual_message_id", None),
        ("source_sender", "moly"),
        ("message_sender", "moly"),
        ("message_kind", "safety"),
        ("message_kind", "safety_check"),
        ("message_kind", "fortune_derived"),
        ("authority", "inferred"),
        ("authority", "confirmed_user"),
        ("start_utf8", 0),
        ("end_utf8", 9999),
        ("span_hash", "changed"),
        ("message_content", "prefix likes tea suffix CHANGED OUTSIDE QUOTE"),
    ],
)
async def test_original_evidence_change_or_replacement_is_rejected(env, field, value):
    state, session = env
    snapshots = await capture(env)
    state["sources"][0][field] = value
    assert not await valid(env, snapshots)


async def test_removed_original_source_cannot_be_replaced_by_new_valid_source(env):
    state, session = env
    snapshots = await capture(env)
    state["sources"] = [source(snapshots[0].registry_id, message_id=2)]
    assert not await valid(env, snapshots)


async def test_valid_additional_source_is_allowed_but_invalid_addition_is_not(env):
    state, session = env
    snapshots = await capture(env)
    state["sources"].append(source(snapshots[0].registry_id, message_id=2))
    assert await valid(env, snapshots)
    state["sources"][-1]["message_kind"] = "safety"
    assert not await valid(env, snapshots)


async def test_source_time_metadata_changes_are_compared_by_actual_rendered_label(env):
    state, session = env
    snapshots = await capture(env)
    old = snapshots[0].occurred_at
    state["sources"][0]["source_occurred_at"] = old + timedelta(hours=1)
    state["rows"][0]["occurred_at"] = old + timedelta(hours=1)
    assert await valid(env, snapshots)  # same Today label
    state["rows"][0]["occurred_at"] = old - timedelta(days=1)
    assert not await valid(env, snapshots)


@pytest.mark.parametrize("language", ["ko", "en", "ja"])
async def test_rendered_time_uses_same_locale_day_and_timezone(env, language):
    state, session = env
    snapshots = await provided.capture(
        session,
        user_id=state["owner"],
        generation=2,
        ids=[state["rows"][0]["registry_id"]],
        language=language,
        today=date(2026, 11, 4),
        tz_name="UTC",
    )
    assert snapshots[0].language == language and snapshots[0].when_label
    assert await valid(env, snapshots)
    state["rows"][0]["occurred_at"] -= timedelta(days=1)
    assert not await valid(env, snapshots)


async def test_without_render_context_time_is_strict(env):
    state, session = env
    snapshots = await provided.capture(
        session, user_id=state["owner"], generation=2, ids=[state["rows"][0]["registry_id"]]
    )
    state["rows"][0]["occurred_at"] += timedelta(minutes=1)
    assert not await valid(env, snapshots)


@pytest.mark.parametrize("change", ["generation", "pipeline", "owner", "missing_registry"])
async def test_snapshot_owner_generation_and_live_registry_are_mandatory(env, change):
    state, session = env
    snapshots = await capture(env)
    if change == "generation":
        state["generation"] += 1
    elif change == "pipeline":
        state["ready"] = False
    elif change == "missing_registry":
        state["rows"] = []
    else:
        snapshots = (replace(snapshots[0], user_id=uuid.uuid4()),)
    assert not await valid(env, snapshots)


@pytest.mark.parametrize("pattern", ["한글🙂", "日本語🙂", "e\u0301🙂", "fullwidth ＡＢＣ"])
async def test_unicode_span_is_validated_on_sanitized_bytes_with_full_raw_identity(env, pattern):
    state, session = env
    rid = state["rows"][0]["registry_id"]
    normalized = memory.sanitize_text(pattern)
    state["sources"] = [source(rid, content=f"before {pattern} after", quote=normalized)]
    snapshots = await capture(env)
    assert await valid(env, snapshots)
    state["sources"][0]["message_content"] += " changed"
    assert not await valid(env, snapshots)


async def test_empty_optional_set_never_queries_unrelated_required(env):
    state, session = env
    assert await provided.capture(session, user_id=state["owner"], generation=2, ids=[]) == ()
    assert await valid(env, ())
    assert state["calls"] == []


async def test_capture_is_immutable_and_does_not_return_partial_invalid_evidence(env):
    state, session = env
    snapshots = await capture(env)
    with pytest.raises(FrozenInstanceError):
        snapshots[0].canonical_text = "changed"
    state["sources"].append(
        {**source(snapshots[0].registry_id, message_id=2), "authority": "inferred"}
    )
    with pytest.raises(provided.ProvidedMemoryError):
        await capture(env)


async def test_database_errors_are_not_misreported_as_success(env):
    state, session = env
    snapshots = await capture(env)

    async def failed(*args, **kwargs):
        raise RuntimeError("synthetic database error")

    session.execute = failed
    with pytest.raises(RuntimeError):
        await valid(env, snapshots)


def test_queries_are_provided_only_no_locks_and_actual_message_sender_is_loaded():
    assert "r.id=ANY(CAST(:ids AS uuid[]))" in str(provided._ROWS)
    assert "MAX(ms.source_occurred_at)" in str(provided._ROWS)
    assert "COALESCE(r.event_started_at,s.occurred_at,r.created_at)" in str(provided._ROWS)
    assert "m.sender AS message_sender" in str(provided._SOURCES)
    assert "LEFT JOIN messages" in str(provided._SOURCES)
    assert "FOR UPDATE" not in str(provided._ROWS) + str(provided._SOURCES)


async def test_valid_same_quote_relocation_still_counts_as_source_replacement(env):
    state, session = env
    rid = state["rows"][0]["registry_id"]
    state["sources"] = [source(rid, content="likes tea and likes tea")]
    snapshots = await capture(env)
    state["sources"][0].update(start_utf8=14, end_utf8=23)
    # The new span is independently valid and byte-identical; its identity changed.
    assert not await valid(env, snapshots)


@pytest.mark.parametrize(
    "field,value",
    [
        ("canonical_text", None),
        ("canonical_text", ""),
        ("canonical_text", "   "),
        ("provider_memory_id", None),
        ("provider", ""),
    ],
)
async def test_incomplete_registry_cannot_be_captured(env, field, value):
    state, session = env
    state["rows"][0][field] = value
    with pytest.raises(provided.ProvidedMemoryError):
        await capture(env)


async def test_missing_source_link_is_not_ignored_when_another_link_is_valid(env):
    state, session = env
    rid = state["rows"][0]["registry_id"]
    state["sources"].append({**source(rid, message_id=2), "actual_message_id": None})
    with pytest.raises(provided.ProvidedMemoryError):
        await capture(env)


async def test_missing_second_fact_never_yields_a_partial_snapshot(env):
    state, session = env
    expected = object()
    result = expected
    with pytest.raises(provided.ProvidedMemoryError):
        result = await provided.capture(
            session,
            user_id=state["owner"],
            generation=2,
            ids=[state["rows"][0]["registry_id"], uuid.uuid4()],
        )
    assert result is expected


async def test_invalid_second_fact_never_yields_a_partial_snapshot(env):
    state, session = env
    second = {
        **copy.deepcopy(state["rows"][0]),
        "registry_id": uuid.uuid4(),
        "canonical_text": "changed",
    }
    state["rows"].append(second)
    state["sources"].append(source(second["registry_id"], message_id=2))
    result = None
    with pytest.raises(provided.ProvidedMemoryError):
        result = await provided.capture(
            session,
            user_id=state["owner"],
            generation=2,
            ids=[r["registry_id"] for r in state["rows"]],
        )
    assert result is None


@pytest.mark.parametrize("operation", ["capture", "validate"])
async def test_cancellation_during_evidence_read_propagates_without_partial_proof(env, operation):
    import asyncio

    state, session = env
    snapshots = await capture(env)
    execute = session.execute

    async def cancelled(sql, params):
        if sql is provided._SOURCES:
            raise asyncio.CancelledError
        return await execute(sql, params)

    session.execute = cancelled
    result = None
    with pytest.raises(asyncio.CancelledError):
        if operation == "capture":
            result = await capture(env)
        else:
            result = await valid(env, snapshots)
    assert result is None


async def test_empty_or_tampered_evidence_proof_cannot_certify_provided_fact(env):
    state, session = env
    snapshots = await capture(env)
    assert not await valid(env, (replace(snapshots[0], sources=frozenset()),))
    assert not await valid(env, (replace(snapshots[0], when_label="changed"),))
    assert not await valid(env, (replace(snapshots[0], exact_text_hash="changed"),))
    assert not await valid(env, (snapshots[0], snapshots[0]))


async def test_snapshot_iterators_do_not_disappear_during_type_validation(env):
    state, session = env
    snapshots = await capture(env)
    state["rows"][0]["semantic_status"] = "superseded"
    assert not await valid(env, iter(snapshots))


@pytest.mark.parametrize("generation", [True, -1, "2"])
async def test_invalid_generation_rejected_before_query(env, generation):
    state, session = env
    with pytest.raises(provided.ProvidedMemoryError):
        await provided.capture(
            session,
            user_id=state["owner"],
            generation=generation,
            ids=[state["rows"][0]["registry_id"]],
        )
    assert not state["calls"]


async def test_display_uses_requested_timezone_and_label_bucket_not_raw_timestamp(env):
    state, session = env
    state["rows"][0]["occurred_at"] = datetime(2026, 11, 3, 20, tzinfo=timezone.utc)
    snapshots = await provided.capture(
        session,
        user_id=state["owner"],
        generation=2,
        ids=[state["rows"][0]["registry_id"]],
        language="ko",
        today=date(2026, 11, 4),
        tz_name="Asia/Seoul",
    )
    assert snapshots[0].when_label == "오늘"
    state["rows"][0]["occurred_at"] += timedelta(hours=3)
    assert await valid(env, snapshots)
    state["rows"][0]["occurred_at"] -= timedelta(days=1)
    assert not await valid(env, snapshots)


@pytest.fixture
def publication(env):
    state, session = env
    state.update(
        lock_events=[], savepoints=[], local_timeout="original", known_messages={1, 2, 3, 4}
    )
    execute = session.execute

    class Savepoint:
        async def __aenter__(self):
            self.rolled_back = False
            self.previous_timeout = state["local_timeout"]
            state["savepoints"].append(self)
            state["lock_events"].append("savepoint")
            return self

        async def rollback(self):
            if not self.rolled_back:
                self.rolled_back = True
                state["local_timeout"] = self.previous_timeout
                state["lock_events"].append("rollback")

        async def __aexit__(self, typ, value, tb):
            if typ is not None:
                await self.rollback()
            elif not self.rolled_back:
                state["lock_events"].append("release")

    session.begin_nested = Savepoint

    async def locked_execute(sql, params):
        name = {
            id(provided._SET_PUBLISH_LOCK_TIMEOUT): "timeout",
            id(provided._CURRENT_SOURCE_MESSAGES): "current_sources",
            id(provided._LOCK_MESSAGES): "messages_share",
            id(provided._LOCK_REGISTRY): "registry_update",
            id(provided._LOCK_SOURCES): "sources_share",
        }.get(id(sql))
        if name is None:
            return await execute(sql, params)
        state["lock_events"].append(name)
        hook = state.get("before_" + name)
        if hook:
            await hook()
        if sql is provided._SET_PUBLISH_LOCK_TIMEOUT:
            state["local_timeout"] = params["lock_timeout"]
            rows = []
        elif sql is provided._CURRENT_SOURCE_MESSAGES:
            rows = [(s["source_message_id"],) for s in state["sources"]]
        elif sql is provided._LOCK_MESSAGES:
            state.setdefault("locked_message_sets", []).append(set(params["message_ids"]))
            rows = [
                (identity,)
                for identity in params["message_ids"]
                if identity in state["known_messages"]
            ]
        elif sql is provided._LOCK_REGISTRY:
            rows = [(r["registry_id"],) for r in state["rows"] if r["registry_id"] in params["ids"]]
        else:
            rows = [(s["source_message_id"],) for s in state["sources"]]
        return SimpleNamespace(all=lambda: rows)

    session.execute = locked_execute
    return state, session


async def publish_validate(publication, snapshots, deadline=None):
    import time

    state, session = publication
    return await provided.validate_for_publish(
        session,
        user_id=state["owner"],
        generation=2,
        snapshots=snapshots,
        deadline=time.monotonic() + 2 if deadline is None else deadline,
    )


async def test_publication_lock_order_and_success_preserve_locks_and_local_timeout(publication):
    state, session = publication
    snapshots = await capture(publication)
    assert await publish_validate(publication, snapshots)
    assert state["lock_events"] == [
        "savepoint",
        "timeout",
        "current_sources",
        "messages_share",
        "registry_update",
        "sources_share",
        "release",
    ]
    assert not state["savepoints"][0].rolled_back
    assert 1 <= int(state["local_timeout"].removesuffix("ms")) <= 250
    assert "ORDER BY id FOR SHARE" in str(provided._LOCK_MESSAGES)
    assert "ORDER BY id FOR UPDATE" in str(provided._LOCK_REGISTRY)
    assert (
        "ORDER BY registry_id,source_message_id,evidence_start_utf8,evidence_end_utf8 FOR SHARE"
        in str(provided._LOCK_SOURCES)
    )
    assert all(
        "NOWAIT" not in str(sql) and "SKIP LOCKED" not in str(sql)
        for sql in [provided._LOCK_MESSAGES, provided._LOCK_REGISTRY, provided._LOCK_SOURCES]
    )


async def test_new_source_between_registry_and_source_lock_restarts_from_message_locks(publication):
    state, session = publication
    snapshots = await capture(publication)

    async def insert_source():
        if len(state["sources"]) == 1:
            state["sources"].append(source(snapshots[0].registry_id, message_id=2))

    state["before_sources_share"] = insert_source
    assert await publish_validate(publication, snapshots)
    assert state["locked_message_sets"] == [{1}, {1, 2}]
    assert len(state["savepoints"]) == 2 and state["savepoints"][0].rolled_back
    assert not state["savepoints"][1].rolled_back
    assert state["lock_events"].index("rollback") < state["lock_events"].index("savepoint", 1)


async def test_existing_additional_source_pk_move_cannot_bypass_message_lock(publication):
    state, session = publication
    snapshots = await capture(publication)
    state["sources"].append(source(snapshots[0].registry_id, message_id=2))

    async def relocate_source():
        state["sources"][-1].update(source_message_id=3, actual_message_id=3)

    state["before_sources_share"] = relocate_source
    assert await publish_validate(publication, snapshots)
    assert state["locked_message_sets"] == [{1, 2}, {1, 3}]
    assert state["savepoints"][0].rolled_back


async def test_membership_changes_twice_fail_without_extra_attempt_or_out_of_order_lock(
    publication,
):
    state, session = publication
    snapshots = await capture(publication)

    async def change_membership():
        state["sources"].append(
            source(snapshots[0].registry_id, message_id=len(state["sources"]) + 1)
        )

    state["before_sources_share"] = change_membership
    assert not await publish_validate(publication, snapshots)
    assert len(state["savepoints"]) == 2 and all(s.rolled_back for s in state["savepoints"])
    assert state["locked_message_sets"] == [{1}, {1, 2}]
    assert state["local_timeout"] == "original"


@pytest.mark.parametrize(
    "change", ["retired", "message_missing", "registry_missing", "source_invalid"]
)
async def test_invalid_final_proof_rolls_back_all_new_locks(publication, change):
    state, session = publication
    snapshots = await capture(publication)
    if change == "retired":
        state["rows"][0]["semantic_status"] = "superseded"
    elif change == "message_missing":
        state["known_messages"].clear()
    elif change == "registry_missing":
        state["rows"].clear()
    else:
        state["sources"][0]["message_content"] += " changed outside quote"
    assert not await publish_validate(publication, snapshots)
    assert state["savepoints"] and all(s.rolled_back for s in state["savepoints"])
    assert state["local_timeout"] == "original"


@pytest.mark.parametrize("state_code", ["55P03", "40P01", "57014"])
@pytest.mark.parametrize("link", ["orig", "__cause__", "__context__"])
async def test_wrapped_database_lock_conflict_returns_false_after_savepoint_rollback(
    publication, state_code, link
):
    state, session = publication
    snapshots = await capture(publication)
    wrapped, original = RuntimeError("synthetic wrapper"), RuntimeError("synthetic driver")
    original.sqlstate = state_code
    setattr(wrapped, link, original)

    async def conflict():
        raise wrapped

    state["before_registry_update"] = conflict
    assert provided.is_lock_conflict(wrapped)
    assert not await publish_validate(publication, snapshots)
    assert state["savepoints"][0].rolled_back
    assert state["local_timeout"] == "original"


async def test_other_database_failure_and_parent_cancel_propagate(publication):
    import asyncio

    state, session = publication
    snapshots = await capture(publication)
    for failure in [RuntimeError("synthetic non-lock failure"), asyncio.CancelledError()]:

        async def fail():
            raise failure

        state["before_registry_update"] = fail
        with pytest.raises(type(failure)):
            await publish_validate(publication, snapshots)
        assert state["savepoints"][-1].rolled_back


async def test_local_total_timeout_fails_but_unrelated_timeout_propagates(publication):
    import asyncio
    import time

    state, session = publication
    snapshots = await capture(publication)

    async def stall():
        await asyncio.Event().wait()

    state["before_registry_update"] = stall
    started = time.monotonic()
    assert not await publish_validate(publication, snapshots, deadline=started + 0.02)
    assert time.monotonic() - started < 0.2 and state["savepoints"][-1].rolled_back

    async def unrelated():
        raise TimeoutError("synthetic non-deadline timeout")

    state["before_registry_update"] = unrelated
    with pytest.raises(TimeoutError):
        await publish_validate(publication, snapshots)


async def test_empty_publication_proof_does_no_sql_and_expired_real_proof_does_not_lock(
    publication,
):
    import time

    state, session = publication
    assert await publish_validate(publication, (), deadline=0)
    assert not state["lock_events"] and not state["calls"]
    snapshots = await capture(publication)
    assert not await publish_validate(publication, snapshots, deadline=time.monotonic() - 1)
    assert not state["savepoints"]


def test_lock_conflict_chain_cycles_terminate_and_do_not_classify_other_errors():
    a, b = RuntimeError(), RuntimeError()
    a.orig, b.orig = b, a
    assert not provided.is_lock_conflict(a)
    b.pgcode = "55P03"
    assert provided.is_lock_conflict(a)
