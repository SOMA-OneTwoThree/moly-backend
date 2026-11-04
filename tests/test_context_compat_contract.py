"""Existing contract read adapter and future-only mutations; no DB/provider access."""
import copy
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from app.services import context_compat as compat, contract_repo as repo
from app.services import contract_compiler as compiler, interaction_contract as ic, llm
from worker import contract_jobs as worker
from worker.consumer import JobCancelled
from tests.test_background_context_budget import job


UID = uuid.uuid4()
OLD_ID = uuid.uuid4()
BODY = "앞으로 자세하게 답해 줘"
END = "그 말투 이제 그만 써줘"
OLD = [{"kind": "response_style", "action": "prefer", "condition": "always",
        "polarity": "positive", "target_tag": "brief", "source_message_id": 20},
       {"kind": "address", "action": "use", "condition": "always",
        "polarity": "positive", "target_literal": "친구", "source_message_id": 30}]


def baseline(document=OLD, **changes):
    values = dict(baseline_id=str(uuid.uuid4()), user_id=str(UID), generation=0,
        privacy_epoch=0, source_after=100, legacy_anchor=100,
        legacy_checkpoint_id=None, legacy_checkpoint_through=0,
        legacy_checkpoint_hash=None, legacy_checkpoint_version=None, legacy_summary_hash=None,
        legacy_contract_after=10, legacy_contract_id=str(OLD_ID),
        legacy_contract_hash=compat.digest(document))
    values.update(changes)
    return compat.Baseline(**values)


def source(body=BODY, mid=110):
    return SimpleNamespace(id=mid, sender="user", content=body, activity_date=date(2026, 11, 5),
                           start_utf8=0, source_hash=hashlib.sha256(body.encode()).hexdigest())


def candidate(previous, **changes):
    raw = dict(kind="response_style", action="prefer", condition="always", target_tag="detailed",
        scope="persistent", source_message_id=110, evidence_quote=BODY,
        supersedes_item_key=previous[0]["item_key"])
    raw.update(changes)
    kept, dropped = compiler.parse(json.dumps([raw]), user_message_ids={110},
        source_messages=[source()], existing_items=previous)
    assert not dropped and len(kept) == 1
    return kept[0]


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)
    def first(self):
        return self.rows[0] if self.rows else None
    def all(self):
        return list(self.rows)


class Session:
    def __init__(self, b, document=OLD):
        self.baseline = b
        self.document = copy.deepcopy(document)
        self.legacy_id = OLD_ID
        self.head = None
        self.sources = {110: BODY, 111: END}
        self.cursor = 10
        self.messages = [(110, "user", BODY, date(2026, 11, 5))]
        self.inserts = []
        self.message_queries = []
        self.calls = 0
        self.writes = []
    async def get(self, model, key):
        return SimpleNamespace(memory_generation=0, retention_compat_json=self.baseline.payload())
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def execute(self, statement, params):
        sql = str(statement)
        if statement in (worker._RETENTION_STATE, worker._RETENTION_STATE_LOCK):
            return Result([(0, 0, self.cursor)])
        if statement is worker._RETENTION_MESSAGES:
            self.message_queries.append(params.copy())
            return Result([row for row in self.messages if params["after"] < row[0] <= params["through"]])
        if statement is worker._PROFILE:
            return Result([(None, "ko")])
        if statement is repo._VALID_SOURCES:
            return Result([(mid, self.sources[mid]) for mid in params["ids"] if mid in self.sources])
        if statement is repo._PENDING_CONVERSATION_ENDS:
            return Result([])
        if "SELECT c.document_json,c.id" in sql:
            return Result(([(copy.deepcopy(OLD), OLD_ID)] if self.baseline.legacy_contract_id and self.legacy_id != OLD_ID else [])
                + ([(self.document, self.legacy_id)] if self.document is not None else []))
        if "SELECT id,document_json,validation_json" in sql:
            return Result([self.head] if self.head else [])
        if "SELECT contract_generation" in sql:
            return Result([(0, self.cursor)])
        if statement is worker._RETENTION_ADVANCE:
            self.cursor = params["through"]
            self.writes.append("advance")
        elif statement is worker._INSERT_ITEM:
            self.writes.append("item")
        return Result()
    async def scalar(self, statement, params):
        if statement is worker._NEXT_VERSION:
            return 2
        if statement is worker._RETENTION_INSERT:
            self.inserts.append(params.copy())
            return uuid.uuid4()
        return 0


async def save_projection(session, b, items, through=111):
    native, metadata = repo.compatibility_metadata(items, b, after=100, through=through,
        source_rows=[(mid, "user", body, date(2026, 11, 5)) for mid, body in session.sources.items()])
    session.head = (uuid.uuid4(), native, metadata)
    return native, metadata


async def test_legacy_generationless_document_is_read_without_rewrite_on_and_off():
    b = baseline()
    session = Session(b)
    for enabled in (True, False):
        rendered = await repo.published_text(session, user_id=UID, locale="ko", generation=0,
            retention_enabled=enabled, compat_baseline=b)
        assert rendered and "친구" in rendered
    items = await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)
    assert len(items) == 2 and all("generation" not in item and "source_hash" not in item for item in items)
    assert session.document == OLD and session.writes == []


async def test_logical_floor_does_not_advance_the_actual_cursor():
    b = baseline()
    session = Session(b)
    assert await repo.effective_contract_cursor(session, user_id=UID, generation=0, compat_baseline=b) == 100
    assert session.cursor == 10
    session.cursor = 120
    assert await repo.effective_contract_cursor(session, user_id=UID, generation=0, compat_baseline=b) == 120


def test_semantic_identity_survives_new_legacy_row_and_source_reference():
    b = baseline()
    later = copy.deepcopy(OLD)
    later[0]["source_message_id"] = 60
    assert repo.legacy_items(OLD, b)[0]["item_key"] == repo.legacy_items(later, b, check_hash=False)[0]["item_key"]


@pytest.mark.parametrize("body", [END, "stop that tone", "その口調をやめて"])
async def test_explicit_legacy_stop_has_a_real_normal_span_and_keeps_other_preferences(body):
    b = baseline()
    session = Session(b)
    session.sources[111] = body
    original = repo.legacy_items(OLD, b)
    merged = repo.apply_legacy_ends(original, [source(body, 111)])
    native, metadata = await save_projection(session, b, merged)
    assert native == [] and len(metadata["legacy_overrides"]) == 1
    loaded = await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)
    assert [item["kind"] for item in repo.active_retained_items(loaded)] == ["address"]
    assert session.document == OLD


@pytest.mark.parametrize("body", ["이번 대화는 여기까지 하자", "회사 이름은 정정할게", "그 말투 그만 쓰지 마", '"그 말투 그만 써"라고 했대'])
def test_unrelated_correction_conversation_end_negation_quote_do_not_clear_legacy(body):
    b = baseline()
    items = repo.legacy_items(OLD, b)
    assert repo.apply_legacy_ends(items, [source(body, 111)]) == items


async def test_new_explicit_correction_replaces_only_one_old_preference_and_keeps_native_proof():
    b = baseline()
    session = Session(b)
    previous = repo.legacy_items(OLD, b)
    merged = worker.merge_retained_items(previous, [candidate(previous)], generation=0)
    native, metadata = await save_projection(session, b, merged)
    assert len(native) == 1 and native[0]["generation"] == 0 and len(metadata["legacy_overrides"]) == 1
    loaded = await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)
    active = repo.active_retained_items(loaded)
    assert len(active) == 2 and {item.get("target_tag") for item in active} == {None, "detailed"}
    assert session.document == OLD


async def test_late_legacy_result_does_not_resurrect_stopped_semantic_item():
    b = baseline()
    session = Session(b)
    merged = repo.apply_legacy_ends(repo.legacy_items(OLD, b), [source(END, 111)])
    await save_projection(session, b, merged)
    session.legacy_id = uuid.uuid4()
    session.document[0]["source_message_id"] = 50
    loaded = await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)
    assert [item["kind"] for item in repo.active_retained_items(loaded)] == ["address"]
    # Temporarily absent preferences retain the source-proven tombstone.
    session.document = [session.document[1]]
    loaded = await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)
    await save_projection(session, b, loaded)
    session.document = copy.deepcopy(OLD)
    loaded = await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)
    assert [item["kind"] for item in repo.active_retained_items(loaded)] == ["address"]


@pytest.mark.parametrize("fault", ["source_deleted", "changed_body", "wrong_generation", "wrong_baseline", "missing_projection"])
async def test_new_delta_or_override_tampering_fails_without_legacy_resurrection(fault):
    b = baseline()
    session = Session(b)
    merged = repo.apply_legacy_ends(repo.legacy_items(OLD, b), [source(END, 111)])
    await save_projection(session, b, merged)
    if fault == "source_deleted":
        del session.sources[111]  # safety reclassification uses the same missing normal-source result.
    elif fault == "changed_body":
        session.sources[111] = "다른 내용"
    elif fault == "wrong_generation":
        session.head[2]["generation"] = 1
    elif fault == "wrong_baseline":
        session.head[2]["baseline_binding"]["baseline_id"] = str(uuid.uuid4())
    else:
        next(iter(session.head[2]["legacy_overrides"].values())).pop("reference")
    with pytest.raises(ic.ContractViolation):
        await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)


@pytest.fixture
def worker_environment(monkeypatch):
    b = baseline()
    session = Session(b)
    monkeypatch.setattr(worker, "get_sessionmaker", lambda: lambda: session)
    async def yes(*args, **kwargs):
        return True
    monkeypatch.setattr(worker.context_retention, "privacy_epoch_matches", yes)
    monkeypatch.setattr(worker.context_retention, "enabled_now", yes)
    monkeypatch.setattr(worker.context_retention, "adopted_user", yes)
    async def generate(*args, **kwargs):
        session.calls += 1
        return llm.LLMResult("[]", 3, 2, finish_reason="stop")
    monkeypatch.setattr(worker.llm, "generate", generate)
    async def publish(*args, **kwargs):
        values = session.inserts[-1]
        session.head = (kwargs["contract_id"], json.loads(values["doc"]), json.loads(values["validation"]))
        session.writes.append("publish")
    monkeypatch.setattr(repo, "publish", publish)
    return b, session


async def test_actual_worker_uses_only_visible_suffix_and_keeps_old_contract_when_model_omits_it(worker_environment):
    b, session = worker_environment
    request = replace(job({"retention_enabled": True, "memory_generation": 0,
        "through_message_id": 110, "baseline_binding": b.payload()}), user_id=UID)
    result = await worker.handle_contract_compile(request)
    assert session.calls == 1 and session.cursor == 10
    await result.apply_domain(session)
    assert session.cursor == 110 and session.writes.count("publish") == 1
    assert all(values["after"] == 100 for values in session.message_queries)
    assert json.loads(session.inserts[0]["doc"]) == []
    assert session.inserts[0]["validation"] and session.document == OLD
    assert len(repo.active_retained_items(await repo.retained_document(session,
        user_id=UID, generation=0, compat_baseline=b))) == 2


@pytest.mark.parametrize("binding", [None, "stale"])
async def test_actual_worker_does_not_rebind_old_job_to_new_baseline(worker_environment, binding):
    b, session = worker_environment
    request = replace(job({"retention_enabled": True, "memory_generation": 0,
        "through_message_id": 110, "baseline_binding": b.payload() if binding else None}), user_id=UID)
    if binding:
        request.payload["baseline_binding"]["baseline_id"] = str(uuid.uuid4())
    with pytest.raises(JobCancelled, match="baseline_changed"):
        await worker.handle_contract_compile(request)
    assert session.calls == 0 and session.writes == []


async def test_baseline_changed_during_model_does_not_publish(worker_environment):
    b, session = worker_environment
    request = replace(job({"retention_enabled": True, "memory_generation": 0,
        "through_message_id": 110, "baseline_binding": b.payload()}), user_id=UID)
    result = await worker.handle_contract_compile(request)
    session.baseline = replace(b, baseline_id=str(uuid.uuid4()))
    await result.apply_domain(session)
    assert session.writes == [] and session.cursor == 10


async def test_native_correction_can_be_explicitly_stopped_without_reviving_legacy():
    b = baseline()
    session = Session(b)
    previous = repo.legacy_items(OLD, b)
    merged = worker.merge_retained_items(previous, [candidate(previous)], generation=0)
    stopped = repo.apply_legacy_ends(merged, [source(END, 111)])
    native, _ = await save_projection(session, b, stopped)
    assert native[0]["_compat_native_end"]["source_message_id"] == 111
    assert [item["kind"] for item in repo.active_retained_items(await repo.retained_document(
        session, user_id=UID, generation=0, compat_baseline=b))] == ["address"]


async def test_replacing_a_new_delta_does_not_invalidate_the_old_legacy_tombstone():
    b = baseline()
    session = Session(b)
    previous = repo.legacy_items(OLD, b)
    merged = worker.merge_retained_items(previous, [candidate(previous)], generation=0)
    await save_projection(session, b, merged)
    native = next(item for item in merged if not item.get("_compat_legacy"))
    new = replace(candidate(previous), source_message_id=112, supersedes_item_key=native["item_key"],
        directive=ic.Directive(ic.Kind.RESPONSE_STYLE, ic.Action.PREFER, ic.Condition.ALWAYS,
            target_tag="casual", polarity=ic.Polarity.POSITIVE), source_hash=hashlib.sha256("반말로 해 줘".encode()).hexdigest(),
        start_utf8=0, end_utf8=len("반말로 해 줘".encode()))
    session.sources[112] = "반말로 해 줘"
    merged = worker.merge_retained_items(merged, [new], generation=0)
    await save_projection(session, b, merged, through=112)
    loaded = await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)
    assert {item.get("target_tag") for item in repo.active_retained_items(loaded)} == {None, "casual"}


async def test_duplicate_confirmation_has_only_one_effective_directive():
    b = baseline()
    previous = repo.legacy_items(OLD, b)
    confirmed = candidate(previous, target_tag="brief", supersedes_item_key=None)
    merged = worker.merge_retained_items(previous, [confirmed], generation=0)
    assert len([item for item in repo.active_retained_items(merged) if item["kind"] == "response_style"]) == 1


async def test_worker_persists_explicit_stop_even_when_model_returns_empty(worker_environment):
    b, session = worker_environment
    session.messages = [(111, "user", END, date(2026, 11, 5))]
    request = replace(job({"retention_enabled": True, "memory_generation": 0,
        "through_message_id": 111, "baseline_binding": b.payload()}), user_id=UID)
    result = await worker.handle_contract_compile(request)
    await result.apply_domain(session)
    assert session.cursor == 111 and session.calls == 1
    assert [item["kind"] for item in repo.active_retained_items(await repo.retained_document(
        session, user_id=UID, generation=0, compat_baseline=b))] == ["address"]


async def test_partial_worker_preserves_baseline_and_does_not_advance_or_publish(worker_environment, monkeypatch):
    b, session = worker_environment
    session.messages = [(110, "user", "🙂" * 1300, date(2026, 11, 5))]
    session.sources[110] = session.messages[0][2]
    queued = []
    async def enqueue(*args, **kwargs):
        queued.append(kwargs)
    from app.services import jobs
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    payload = {"retention_enabled": True, "memory_generation": 0, "through_message_id": 110,
        "baseline_binding": b.payload(), "adaptive_output_fallback": True,
        "adaptive_output_attempts_remaining": 3}
    async def persist(*args, **kwargs):
        pass
    monkeypatch.setattr(worker, "_persist_output_fallback", persist)
    request = replace(job(payload), user_id=UID)
    result = await worker.handle_contract_compile(request)
    await result.apply_domain(session)
    assert session.cursor == 10 and not session.inserts
    assert queued[0]["payload"]["baseline_binding"] == b.payload()
    assert queued[0]["payload"]["partial_offset"] > 0
    assert all("generation" not in item for item in queued[0]["payload"]["partial_items"])


async def test_current_stop_is_effective_before_worker_and_unrelated_pref_is_kept():
    b = baseline()
    session = Session(b)
    text = await repo.published_text(session, user_id=UID, generation=0, locale="ko",
        retention_enabled=True, compat_baseline=b, current_text=END)
    assert "brief" not in text and "친구" in text


async def test_pending_stop_beyond_the_first_two_hundred_rows_is_not_lost():
    b = baseline()
    class PendingSession(Session):
        async def execute(self, statement, params):
            if statement is repo._PENDING_CONVERSATION_ENDS:
                rows = [(mid, "일반 대화") for mid in range(101, 301)] + [(301, END)]
                return Result([row for row in rows if row[0] > params["after"]][:200])
            return await super().execute(statement, params)
    session = PendingSession(b)
    text = await repo.published_text(session, user_id=UID, generation=0, locale="ko",
        retention_enabled=True, compat_baseline=b)
    assert "brief" not in text and "친구" in text


@pytest.mark.parametrize("changes", [{"generation": 1}, {"user_id": str(uuid.uuid4())}])
async def test_wrong_generation_or_owner_cannot_read_a_legacy_baseline(changes):
    b = baseline(**changes)
    with pytest.raises(ic.ContractViolation):
        await repo.retained_document(Session(b), user_id=UID, generation=0, compat_baseline=b)


async def test_no_old_contract_still_publishes_an_empty_proven_new_range(worker_environment):
    _, session = worker_environment
    b = baseline(legacy_contract_id=None, legacy_contract_hash=None)
    session.baseline = b
    session.document = None
    request = replace(job({"retention_enabled": True, "memory_generation": 0,
        "through_message_id": 110, "baseline_binding": b.payload()}), user_id=UID)
    result = await worker.handle_contract_compile(request)
    await result.apply_domain(session)
    assert session.cursor == 110 and session.calls == 1
    assert await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b) == []


async def test_scope_day_still_expires_normally_with_legacy_preferences():
    b = baseline()
    session = Session(b)
    previous = repo.legacy_items(OLD, b)
    kept = candidate(previous, scope="day", supersedes_item_key=None)
    merged = worker.merge_retained_items(previous, [kept], generation=0)
    await save_projection(session, b, merged)
    loaded = await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)
    assert len(repo.active_retained_items(loaded, activity_date=date(2026, 11, 5))) == 3
    assert len(repo.active_retained_items(loaded, activity_date=date(2026, 11, 6))) == 2


@pytest.mark.parametrize("body,closed", [
    (BODY + ". " + END, True), (END + ". " + BODY, False),
])
async def test_same_message_native_persistent_end_requires_a_later_byte_span(body, closed):
    b = baseline()
    session = Session(b)
    session.sources[110] = body
    previous = repo.legacy_items(OLD, b)
    raw = dict(kind="response_style", action="prefer", condition="always", target_tag="detailed",
        scope="persistent", source_message_id=110, evidence_quote=BODY,
        supersedes_item_key=previous[0]["item_key"])
    kept, dropped = compiler.parse(json.dumps([raw]), user_message_ids={110},
        source_messages=[source(body)], existing_items=previous)
    assert not dropped
    merged = repo.apply_legacy_ends(worker.merge_retained_items(previous, kept, generation=0), [source(body)])
    await save_projection(session, b, merged)
    loaded = await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)
    assert bool([item for item in repo.active_retained_items(loaded) if item["kind"] == "response_style"]) is not closed


def test_legacy_latest_head_query_is_bound_to_the_original_locale():
    import inspect
    sql = inspect.getsource(repo._legacy_items)
    assert "c.locale=COALESCE((SELECT locale FROM user_interaction_contracts WHERE id=:id)" in sql
    assert "FROM profiles WHERE id=:user_id" in sql


async def test_late_legacy_extraction_omission_cannot_erase_original_preference():
    b = baseline()
    session = Session(b)
    session.legacy_id = uuid.uuid4()
    session.document = [copy.deepcopy(OLD[0])]
    loaded = await repo.retained_document(session, user_id=UID, generation=0, compat_baseline=b)
    assert {item["kind"] for item in repo.active_retained_items(loaded)} == {"address", "response_style"}
    # Save all observed legacy document IDs, including entries not picked as the first head.
    _, metadata = await save_projection(session, b, loaded)
    assert set(metadata["legacy_contract_ids"]) == {str(OLD_ID), str(session.legacy_id)}
