"""Durable contract processing proof survives job retention and validates actual source prefixes."""
import copy
from datetime import date
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import interaction_contract as ic, retention_release as release
from app.services.retention_preparation_context import Binding, use_binding


@pytest.fixture
def chain():
    value, owner = Binding(uuid.uuid4(), True), uuid.uuid4()
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    first = [(2, "user", "synthetic first 🙂", date(2026, 11, 4)),
             (3, "moly", "synthetic reply", date(2026, 11, 4))]
    second = [(7, "user", "synthetic second", date(2026, 11, 4)),
              (10, "moly", "synthetic response", date(2026, 11, 4))]
    document = []
    digest = ic.document_hash(json.dumps(document, ensure_ascii=False, sort_keys=True))
    initial = release.contract_proof(value, generation=0, locale="en", after=0, through=3,
        rows=first, document_hash=digest, parent=None)
    final = release.contract_proof(value, generation=0, locale="en", after=3, through=10,
        rows=second, document_hash=digest, parent=(document, 3, digest, first_id, initial))
    state = {"rows": [(second_id, "en", document, digest, 10, "ready", final),
                      (first_id, "en", document, digest, 3, "superseded", initial)],
             "sources": first+second, "sql": []}
    class Session:
        async def scalar(self, sql, params):
            return True if "SELECT EXISTS" in str(sql) else "building"
        async def execute(self, sql, params):
            state["sql"].append(str(sql))
            assert "async_jobs" not in str(sql)
            if "FROM retention_prepared_contracts" in str(sql):
                assert params["user_id"] == owner
                assert params["preparation_id"] == value.preparation_id
                rows = [row for row in state["rows"] if row[5] in {"ready", "published", "superseded"}]
            else:
                assert "kind='normal'" in str(sql)
                rows = [row for row in state["sources"] if params["after"] < row[0] <= params["through"]]
            return SimpleNamespace(all=lambda: copy.deepcopy(rows))
    return value, owner, state, Session()


async def valid(chain):
    value, owner, _, session = chain
    with use_binding(value):
        return await release.validate_contract_chain(session, user_id=owner, generation=0, through_id=10)


async def test_superseded_parent_chain_validates_with_no_job_payloads(chain):
    assert await valid(chain)
    assert len(chain[2]["sql"]) == 3  # one chain read + each exact normal interval


@pytest.mark.parametrize("change", ["body", "source_missing", "source_added", "activity_date",
    "source_order", "parent_missing", "parent_document", "cycle", "missing_proof", "wrong_preparation",
    "wrong_generation", "wrong_locale", "wrong_ids", "boolean_id", "range_skip", "document_hash"])
async def test_every_source_range_and_document_link_is_verified(chain, change):
    _, _, state, _ = chain
    row = state["rows"][0]
    proof = row[6]
    if change == "body":
        old = state["sources"][0]
        state["sources"][0] = (old[0], old[1], old[2]+" outside evidence", old[3])
    elif change == "source_missing":
        state["sources"].pop(0)
    elif change == "source_added":
        state["sources"].insert(1, (2+0.5, "user", "invalid identity", None))
    elif change == "activity_date":
        old = state["sources"][0]
        state["sources"][0] = (*old[:3], date(2026, 11, 3))
    elif change == "source_order":
        state["sources"][:2] = reversed(state["sources"][:2])
    elif change == "parent_missing":
        state["rows"].pop()
    elif change == "parent_document":
        old = state["rows"][1]
        state["rows"][1] = (*old[:2], [{"changed": True}], *old[3:])
    elif change == "cycle":
        proof["previous_contract_id"] = str(row[0])
    elif change == "missing_proof":
        proof.clear()
    elif change == "wrong_preparation":
        proof["preparation_id"] = str(uuid.uuid4())
    elif change == "wrong_generation":
        proof["generation"] = False
    elif change == "wrong_locale":
        proof["locale"] = "ja"
    elif change == "wrong_ids":
        proof["source_message_ids"] = [10]
    elif change == "boolean_id":
        proof["source_message_ids"] = [True, 10]
    elif change == "range_skip":
        proof["after_message_id"] = 7
    else:
        proof["document_hash"] = "b"*64
    assert not await valid(chain)


@pytest.mark.parametrize("pattern", ["한글🙂", "日本語🙂", 'quote " and newline\n', "repeat repeat"])
def test_source_hash_covers_exact_body_sender_identity_and_day(pattern):
    rows = [(1, "user", pattern, date(2026, 11, 4))]
    original = release.contract_source_hash(rows)
    assert len({original, release.contract_source_hash([(2, "user", pattern, rows[0][3])]),
        release.contract_source_hash([(1, "moly", pattern, rows[0][3])]),
        release.contract_source_hash([(1, "user", pattern+" ", rows[0][3])]),
        release.contract_source_hash([(1, "user", pattern, None)])}) == 5


def test_partial_message_or_missing_parent_cannot_be_certified():
    value = Binding(uuid.uuid4(), True)
    with pytest.raises(release.ReleaseError, match="source_boundary"):
        release.contract_proof(value, generation=0, locale="en", after=0, through=2,
            rows=[(3, "user", "synthetic", None)], document_hash="a"*64, parent=None)
    with pytest.raises(release.ReleaseError, match="parent_missing"):
        release.contract_proof(value, generation=0, locale="en", after=1, through=2,
            rows=[(2, "user", "synthetic", None)], document_hash="a"*64, parent=None)


async def test_draft_is_never_a_processing_head(chain):
    _, _, state, _ = chain
    state["rows"][0] = (*state["rows"][0][:5], "draft", state["rows"][0][6])
    assert not await valid(chain)


def test_uuid_parameter_codec_is_explicit_before_text_comparison():
    from app.services import context_retention
    value = Binding(uuid.uuid4(), True)
    with use_binding(value):
        sql = str(release.job_sql(context_retention._CONTRACT_ACTIVE))
        params = release.params()
    assert "CAST(CAST(:preparation_id AS uuid) AS text)" in sql
    assert isinstance(params["preparation_id"], uuid.UUID)


async def test_database_errors_are_not_misreported_as_verified(chain):
    value, owner, _, session = chain
    session.execute = AsyncMock(side_effect=RuntimeError("synthetic database failure"))
    with use_binding(value), pytest.raises(RuntimeError):
        await release.validate_contract_chain(session, user_id=owner, generation=0, through_id=10)


@pytest.mark.parametrize("empty", [False, True])
async def test_policy_excluded_tail_can_complete_without_becoming_normal_source(chain, empty):
    value, _, state, _ = chain
    last = state["rows"][0]
    sources = [] if empty else [(7, "user", "synthetic second", date(2026, 11, 4))]
    parent = state["rows"][1]
    parent_record = (parent[2], parent[4], parent[3], parent[0], parent[6])
    proof = release.contract_proof(value, generation=0, locale="en", after=3, through=10,
        rows=sources, document_hash=last[3], parent=parent_record)
    state["rows"][0] = (*last[:6], proof)
    state["sources"] = [row for row in state["sources"] if row[0] <= 3] + sources
    assert await valid(chain)


async def test_a_forged_nonexistent_boundary_cannot_advance_coverage(chain):
    _, _, _, session = chain
    async def scalar(sql, params):
        return False if "SELECT EXISTS" in str(sql) else "building"
    session.scalar = scalar
    assert not await valid(chain)
