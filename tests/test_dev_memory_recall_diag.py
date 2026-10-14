"""The read-only diagnostic exposes actual vector recall and bounded candidate cuts."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import dev
from app.core import errors
from app.services import chat, mem0_adapter, mem0_recall, memory_embeddings

UID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(hits=[], registry=[], searches=0, embeds=0, db_calls=0, fail=None)

    class Collection:
        def query(self, **kwargs):
            state.searches += 1
            assert kwargs["filters"] == {"user_id": {"$eq": str(UID)}}
            assert kwargs["limit"] == 40
            if state.fail == "provider":
                raise RuntimeError("do-not-echo-provider-secret")
            # Deliberately ignore provider owner filter; actual adapter must revalidate it.
            return state.hits

    class Session:
        async def execute(self, statement, params):
            state.db_calls += 1
            assert params["user_id"] == UID
            assert params["statuses"] == ["active", "ambiguous"]
            assert "r.user_id = :user_id" in str(statement)
            assert "r.semantic_status = ANY(:statuses)" in str(statement)
            if state.fail == "registry":
                raise RuntimeError("do-not-echo-registry-secret")
            rows = [
                (item["id"], item["status"], item.get("time"), None,
                 item.get("time_basis", "event"), item.get("time_precision"))
                for item in state.registry
                if item["id"] in params["ids"] and item["owner"] == params["user_id"]
                and item["status"] in params["statuses"]
            ]
            return SimpleNamespace(all=lambda: rows)

    adapter = mem0_adapter.Mem0VectorIndexAdapter(None, "synthetic-only")
    adapter._collection = Collection()

    async def run(function, *args, timeout):
        return function(*args)

    async def embed(query, *, timeout):
        state.embeds += 1
        if state.fail == "embedding":
            raise RuntimeError("do-not-echo-embedding-secret")
        return [0.1]

    monkeypatch.setattr(adapter, "_run", run)
    monkeypatch.setattr(chat, "_recall_adapter", lambda: adapter)
    monkeypatch.setattr(memory_embeddings, "embed_query", embed)
    monkeypatch.setattr(dev.settings, "environment", "development")
    app = FastAPI()
    errors.register_error_handlers(app)
    app.include_router(dev.router)
    app.dependency_overrides[dev.require_dev_operator] = lambda: str(UID)
    app.dependency_overrides[dev.get_session] = lambda: Session()
    state.client = TestClient(app)
    state.adapter = adapter
    state.session = Session()
    return state


def add(harness, key, distance, body, *, payload_owner=UID, registry_owner=UID,
        status="active", **kwargs):
    harness.hits.append((key, distance, {"user_id": str(payload_owner), "text": body}))
    harness.registry.append({"id": key, "owner": registry_owner, "status": status, **kwargs})


def query(harness, **kwargs):
    return harness.client.post("/dev/recall/memory", json={
        "query": "그때 무슨 얘기했지?", **kwargs,
    })


def test_actual_recalled_shape_returns_200_and_limit_after_filter(harness):
    for number in range(4):
        add(harness, f"p{number}", 0.3 + number / 100, f"synthetic {number}")
    response = query(harness, limit=2)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"
    assert result["min_keep"] == mem0_recall.MIN_KEEP == 5
    assert result["provider_count"] == result["registry_count"] == 4
    assert result["margin_count"] == result["selected_count"] == 4
    assert result["returned_count"] == len(result["results"]) == 2
    assert result["has_more"] is True
    assert result["results"][0] == {
        "text": "synthetic 0", "status": "active", "distance": 0.3,
        "occurred_at": None, "uncertain": False, "time_basis": "event", "time_precision": None,
    }
    assert "ref" not in result["results"][0]


def test_both_provider_and_registry_owner_filters_and_closed_states_are_preserved(harness):
    add(harness, "own", 0.3, "own")
    add(harness, "provider-leak", 0.31, "foreign provider", payload_owner=OTHER)
    add(harness, "registry-leak", 0.32, "foreign registry", registry_owner=OTHER)
    add(harness, "superseded", 0.33, "old", status="superseded")
    add(harness, "excluded", 0.34, "deleted", status="excluded")
    result = query(harness).json()
    assert [item["text"] for item in result["results"]] == ["own"]
    assert result["provider_count"] == 4
    assert result["registry_count"] == 1


def test_observed_time_and_uncertainty_are_not_invented_entity_metadata(harness):
    at = datetime(2026, 10, 14, 1, 2, tzinfo=timezone.utc)
    add(harness, "ambiguous", 0.3, "uncertain", status="ambiguous",
        time=at, time_basis="mentioned", time_precision="minute")
    item = query(harness).json()["results"][0]
    assert item["occurred_at"] == at.isoformat()
    assert item["uncertain"] is True and item["time_basis"] == "mentioned"


@pytest.mark.parametrize("stage", ["embedding", "provider", "registry"])
def test_failure_is_unavailable_not_empty_match_and_does_not_echo_error(harness, stage, caplog):
    add(harness, "own", 0.3, "own")
    harness.fail = stage
    response = query(harness)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "unavailable"
    assert result["error_code"] == stage + "_unavailable"
    assert result["results"] == [] and result["selected_count"] is None
    assert "do-not-echo" not in response.text
    assert "do-not-echo" not in caplog.text
    assert (result["provider_count"] == 1) if stage == "registry" else (
        result["provider_count"] is None
    )


def test_skipped_query_and_successful_no_match_are_distinct(harness):
    skipped = query(harness, query="안녕").json()
    assert skipped["status"] == "skipped" and skipped["selected_count"] == 0
    assert skipped["provider_count"] is None and harness.searches == harness.embeds == 0
    missing = query(harness).json()
    assert missing["status"] == "ok" and missing["selected_count"] == 0
    assert missing["provider_count"] == 0 and missing["error_code"] is None


def test_minimum_zero_removes_only_outside_margin_filler(harness):
    add(harness, "near", 0.3, "related synthetic")
    add(harness, "filler1", 0.5, "unrelated synthetic 1")
    add(harness, "filler2", 0.6, "unrelated synthetic 2")
    baseline = query(harness, min_keep=5).json()
    candidate = query(harness, min_keep=0).json()
    assert baseline["margin_count"] == candidate["margin_count"] == 1
    assert baseline["selected_count"] == 3 and candidate["selected_count"] == 1
    assert candidate["results"][0]["text"] == "related synthetic"


def test_minimum_zero_can_lose_required_name_outside_margin(harness):
    # Known ranking failure, not evidence a live embedding provider ranks this way.
    for number in range(3):
        add(harness, f"near{number}", 0.3 + number / 1000, f"related detail {number}")
    add(harness, "name", 0.41, "synthetic partner name is Nari")
    baseline = query(harness, min_keep=5, query="내가 말한 이름 기억나?").json()
    candidate = query(harness, min_keep=0, query="내가 말한 이름 기억나?").json()
    assert any("Nari" in item["text"] for item in baseline["results"])
    assert all("Nari" not in item["text"] for item in candidate["results"])
    assert candidate["selected_count"] == 3


def test_no_minimum_still_keeps_unrelated_candidates_inside_margin(harness):
    add(harness, "unrelated1", 0.7, "unrelated synthetic 1")
    add(harness, "unrelated2", 0.701, "unrelated synthetic 2")
    result = query(harness, min_keep=0).json()
    assert result["returned_count"] == 2


@pytest.mark.parametrize("overrides", [
    {"min_keep": 1}, {"min_keep": False}, {"min_keep": -1}, {"min_keep": 6},
    {"limit": 0}, {"limit": 11}, {"need": "exact"}, {"need": "quote"},
    {"from_date": "2026-10-01"}, {"to_date": "2026-10-14"},
])
def test_unsupported_request_is_rejected_before_search(harness, overrides):
    response = query(harness, **overrides)
    assert response.status_code == 422
    assert harness.searches == harness.embeds == harness.db_calls == 0


@pytest.mark.parametrize("environment", ["local", "production", "unknown", ""])
@pytest.mark.parametrize("min_keep", [0, 5])
def test_explicit_candidate_comparison_requires_development(
    harness, monkeypatch, environment, min_keep,
):
    monkeypatch.setattr(dev.settings, "environment", environment)
    response = query(harness, min_keep=min_keep)
    assert response.status_code == 403
    assert harness.searches == harness.embeds == harness.db_calls == 0


def test_adapter_initialization_failure_is_unavailable_not_server_500(harness, monkeypatch):
    def fail():
        raise RuntimeError("private-initialization-detail")

    monkeypatch.setattr(chat, "_recall_adapter", fail)
    response = query(harness)
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["error_code"] == "provider_unavailable"
    assert "private-initialization-detail" not in response.text
    assert harness.searches == harness.embeds == harness.db_calls == 0


def test_real_operator_dependency_denies_memory_diagnostic_before_search(harness, monkeypatch):
    app = harness.client.app
    app.dependency_overrides.pop(dev.require_dev_operator)
    app.dependency_overrides[dev.get_current_user] = lambda: str(UID)
    monkeypatch.setattr(dev.settings, "dev_operator_user_ids", "")
    response = query(harness)
    assert response.status_code == 403
    assert harness.searches == harness.embeds == harness.db_calls == 0


async def test_default_list_callers_keep_minimum_and_failure_contract(harness):
    add(harness, "near", 0.3, "near")
    add(harness, "far", 0.6, "far")
    items = await mem0_recall.recall(
        harness.session, UID, query="그때 무슨 얘기했지?",
        adapter=harness.adapter, embed_query=memory_embeddings.embed_query,
    )
    assert [item.text for item in items] == ["near", "far"]
    harness.fail = "provider"
    assert await mem0_recall.recall(
        harness.session, UID, query="그때 무슨 얘기했지?",
        adapter=harness.adapter, embed_query=memory_embeddings.embed_query,
    ) == []
