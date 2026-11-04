"""Legacy canonical NULL compatibility is read-only, scoped and source certified."""
import hashlib
import uuid
from types import SimpleNamespace

import pytest

from app.services import context_retention as retention, mem0_ingest, provided_memory as provided
from tests import test_provided_memory as provided_tests

env = provided_tests.env
capture = provided_tests.capture
valid = provided_tests.valid


async def test_vector_hit_can_supply_a_hash_matching_legacy_null_in_memory_only(env):
    state, _ = env
    row = state["rows"][0]
    row["canonical_text"] = None
    proof = (await capture(env, canonical_overrides={row["registry_id"]: " likes  tea \n"}))[0]
    assert proof.canonical_text == "likes tea" and proof.canonical_from_legacy
    assert row["canonical_text"] is None and await valid(env, (proof,))
    assert all(not sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for sql, _ in state["calls"])


async def test_null_canonical_without_a_real_hit_is_not_invented(env):
    state, _ = env
    state["rows"][0]["canonical_text"] = None
    with pytest.raises(provided.ProvidedMemoryError):
        await capture(env)


@pytest.mark.parametrize("value", ["likes coffee", "", None, 3])
async def test_hit_must_match_the_registry_content_hash(env, value):
    state, _ = env
    row = state["rows"][0]
    row["canonical_text"] = None
    with pytest.raises(provided.ProvidedMemoryError):
        await capture(env, canonical_overrides={row["registry_id"]: value})


async def test_no_override_for_another_registry_or_user_can_enter_the_snapshot(env):
    state, _ = env
    state["rows"][0]["canonical_text"] = None
    with pytest.raises(provided.ProvidedMemoryError):
        await capture(env, canonical_overrides={uuid.uuid4(): "likes tea"})


@pytest.mark.parametrize("change", ["source_kind", "authority", "source_body", "hash", "generation", "registry"])
async def test_legacy_override_keeps_all_existing_provenance_checks(env, change):
    state, _ = env
    row = state["rows"][0]
    row["canonical_text"] = None
    proof = await capture(env, canonical_overrides={row["registry_id"]: "likes tea"})
    if change == "source_kind":
        state["sources"][0]["message_kind"] = "safety_check"
    elif change == "authority":
        state["sources"][0]["authority"] = "inferred"
    elif change == "source_body":
        state["sources"][0]["message_content"] += " changed"
    elif change == "hash":
        row["content_hash"] = mem0_ingest.candidate_hash("changed")
    elif change == "generation":
        state["generation"] += 1
    else:
        row["registry_id"] = uuid.uuid4()
    assert not await valid(env, proof)


async def test_an_original_canonical_snapshot_cannot_resurrect_a_new_null(env):
    state, _ = env
    proof = await capture(env)
    state["rows"][0]["canonical_text"] = None
    assert not await valid(env, proof)


async def test_equal_canonical_population_is_a_semantically_safe_legacy_transition(env):
    state, _ = env
    row = state["rows"][0]
    row["canonical_text"] = None
    proof = await capture(env, canonical_overrides={row["registry_id"]: "likes tea"})
    row["canonical_text"] = "likes tea"
    assert await valid(env, proof)
    row["canonical_text"] = "likes  tea"
    assert not await valid(env, proof)


class Rows:
    def __init__(self, rows):
        self.rows = rows
    def all(self):
        return self.rows


@pytest.fixture
def required():
    uid, rid = uuid.uuid4(), uuid.uuid4()
    body, fact = "나는 우유 알레르기가 있어", "사용자는 우유 알레르기가 있다"
    digest = mem0_ingest.candidate_hash(fact)
    state = dict(body=body, fact=fact, digest=digest, sources=[(rid, 1, "user", "explicit_user",
        0, len(body.encode()), hashlib.sha256(body.encode()).hexdigest(), body, "normal", "user")],
        candidates=[fact], vector_owner=None, vector_data=None, vector_text=None,
        legacy=True, canonical=None, calls=[])
    class Session:
        async def execute(self, statement, params):
            state["calls"].append((statement, params))
            assert params["user_id"] == uid
            if statement is retention._REQUIRED:
                return Rows([(rid, state["canonical"], 1, state["digest"])])
            assert set(params["ids"]) == {rid}
            if statement is retention._REQUIRED_SOURCES:
                return Rows(state["sources"])
            assert statement is retention._REQUIRED_LEGACY_CANONICAL
            return Rows([(rid, state["digest"], state["candidates"], state["vector_owner"],
                state["vector_data"], state["vector_text"])] if state["legacy"] else [])
    return SimpleNamespace(uid=uid, rid=rid, session=Session(), state=state)


async def required_text(value, **kwargs):
    return await retention.load_required_facts(value.session, user_id=value.uid, generation=0, **kwargs)


async def test_required_null_uses_only_committed_candidate_for_its_exact_subset(required):
    assert await required_text(required) == required.state["fact"]
    assert required.state["canonical"] is None
    assert len(required.state["calls"]) == 3
    sql = str(retention._REQUIRED_LEGACY_CANONICAL)
    assert "r.id=ANY" in sql and "c.status='committed'" in sql and "c.user_id=r.user_id" in sql


async def test_required_null_can_use_owned_vector_text_but_not_foreign_metadata(required):
    required.state["candidates"] = []
    required.state["vector_owner"] = str(required.uid)
    required.state["vector_text"] = required.state["fact"]
    assert await required_text(required) == required.state["fact"]
    required.state["vector_owner"] = str(uuid.uuid4())
    with pytest.raises(retention.RetentionError, match="required_legacy_unproved"):
        await required_text(required)


async def test_required_supplied_hit_does_not_dispatch_an_additional_vector_lookup(required):
    assert await required_text(required,
        canonical_overrides={required.rid: required.state["fact"]}) == required.state["fact"]
    assert len(required.state["calls"]) == 2


@pytest.mark.parametrize("change", ["candidate", "missing", "source_kind", "actual_sender", "authority", "span", "utf8", "body"])
async def test_required_legacy_never_turns_failed_proof_into_empty_memory(required, change):
    state = required.state
    if change == "candidate":
        state["candidates"] = ["다른 내용"]
    elif change == "missing":
        state["legacy"] = False
    else:
        row = list(state["sources"][0])
        if change == "source_kind":
            row[8] = "safety"
        elif change == "actual_sender":
            row[9] = "moly"
        elif change == "authority":
            row[3] = "inferred"
        elif change == "span":
            row[5] = 9999
        elif change == "utf8":
            row[4], row[5] = 1, 2
            row[6] = hashlib.sha256(state["body"].encode()[1:2]).hexdigest()
        else:
            row[7] += " 바뀜"
            row[6] = "0" * 64
        state["sources"] = [tuple(row)]
    with pytest.raises(retention.RetentionError):
        await required_text(required)
