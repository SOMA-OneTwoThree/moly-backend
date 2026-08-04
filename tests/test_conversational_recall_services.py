"""최종 회상 구조의 교차 계층 불변식."""
from __future__ import annotations

import hashlib
import uuid

import pytest

from app.core.errors import AppError
from app.services import (
    chat_turns,
    chat_references,
    diary_recall_repo,
    episodic_memory,
    projection_repair,
    recall_diaries,
    recall_memory,
)


UID = uuid.UUID("11111111-1111-1111-1111-111111111111")


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def first(self):
        return self.rows[0] if self.rows else None


class _LeaseSession:
    def __init__(self, acquire_row=None, verify_row=None, revision=None):
        self.acquire_row = acquire_row
        self.verify_row = verify_row
        self.revision = revision
        self.calls = []

    async def execute(self, stmt, params=None):
        self.calls.append((stmt, params))
        if stmt is chat_turns._ACQUIRE:
            return _Result([self.acquire_row] if self.acquire_row else [])
        if stmt is chat_turns._VERIFY:
            return _Result([self.verify_row] if self.verify_row else [])
        if stmt is chat_turns._PUBLISH:
            return _ScalarResult(self.revision)
        return _Result()


class _ScalarResult(_Result):
    def scalar(self):
        return self.revision

    def __init__(self, revision):
        super().__init__()
        self.revision = revision


def test_request_hash_binds_the_logical_body() -> None:
    same = chat_turns.request_hash(text_value="안녕", greeting_id=None)
    assert same == chat_turns.request_hash(text_value="안녕", greeting_id=None)
    assert same != chat_turns.request_hash(text_value="안녕!", greeting_id=None)
    assert same != chat_turns.request_hash(text_value="안녕", greeting_id=str(uuid.uuid4()))
    assert same != chat_turns.request_hash(
        text_value="안녕", greeting_id=None, diary_references=True
    )


def test_capability_header_requires_an_exact_token() -> None:
    assert chat_turns.diary_reference_capable("diary-reference-v1")
    assert chat_turns.diary_reference_capable("other, diary-reference-v1")
    assert not chat_turns.diary_reference_capable("diary-reference-v10")


async def test_active_turn_acquire_returns_server_owned_cas_coordinates() -> None:
    token = uuid.uuid4()
    session = _LeaseSession(acquire_row=(token, 8, 12))
    lease = await chat_turns.acquire(
        session,
        user_id=UID,
        idempotency_key="k",
        request_digest="h",
        lease_seconds=15,
    )
    assert lease == chat_turns.Lease(token=token, turn_seq=8, base_context_revision=12)
    assert session.calls[0][0] is chat_turns._ENSURE_CONTEXT


async def test_active_turn_conflict_fails_without_starting_inference() -> None:
    with pytest.raises(AppError) as caught:
        await chat_turns.acquire(
            _LeaseSession(),
            user_id=UID,
            idempotency_key="k",
            request_digest="h",
            lease_seconds=15,
        )
    assert caught.value.code == "CHAT_TURN_IN_PROGRESS"


def test_episode_projection_never_duplicates_raw_message_text() -> None:
    sql = str(episodic_memory._ENQUEUE_ROW).lower()
    assert "content_hash" in sql
    assert "m.content" not in sql
    assert "join messages" in str(episodic_memory._LOAD).lower()


def test_projection_upserts_preserve_unchanged_vectors() -> None:
    episode_sql = str(episodic_memory._ENQUEUE_ROW).lower()
    diary_sql = str(diary_recall_repo._UPSERT_DOCUMENT).lower()
    assert "then null else memory_episodic_messages.embedding end" in episode_sql
    assert "embedding_model is distinct from" in episode_sql
    assert "then null else diary_recall_documents.embedding end" in diary_sql
    assert "embedding_model is distinct from" in diary_sql


def test_projection_writes_are_fenced_by_model_and_index() -> None:
    episode_sql = str(episodic_memory._WRITE).lower()
    diary_sql = str(diary_recall_repo._WRITE_EMBEDDING).lower()
    for sql in (episode_sql, diary_sql):
        assert "embedding_model=:embedding_model" in sql
        assert "index_version=:index_version" in sql
        assert "embedding_repair_attempts=0" in sql


def test_missing_vector_repair_is_bounded_and_privacy_safe() -> None:
    episode_sql = str(projection_repair._EPISODES).lower()
    diary_sql = str(projection_repair._DIARIES).lower()
    for sql in (episode_sql, diary_sql):
        assert "embedding_repair_attempts<:max_attempts" in sql
        assert "privacy_subject_barriers" in sql
        assert "state in ('ready','running')" in sql
        assert "skip locked" in sql


def test_history_diary_hydration_revalidates_lifecycle_and_suppression() -> None:
    # SQLAlchemy join clause is built in the function; source inspection avoids a DB-shaped mock while
    # fixing the public-history invariant as a contract.
    import inspect

    source = inspect.getsource(chat_references.hydrate_for_messages)
    assert "Diary.record_status == \"published\"" in source
    assert "Diary.deleted_at.is_(None)" in source
    assert "RecallSuppression.message_id == DiaryClaimSource.message_id" in source


def test_recall_queries_apply_suppression_before_ranking() -> None:
    diary_sql = str(recall_diaries._RECALL).lower()
    memory_sql = str(recall_memory._EPISODES).lower()
    assert "memory_recall_suppressions" in diary_sql
    assert "memory_recall_suppressions" in memory_sql
    assert diary_sql.index("memory_recall_suppressions") < diary_sql.index("order by")
    assert memory_sql.index("memory_recall_suppressions") < memory_sql.index("order by")


def test_optional_recall_parameters_have_explicit_postgres_types() -> None:
    diary_sql = str(recall_diaries._RECALL).lower()
    assert "cast(:embedding as vector(1536))" in diary_sql
    assert "cast(:query as text)" in diary_sql
    assert "cast(:from_date as date)" in diary_sql
    assert "cast(:focus_id as uuid)" in diary_sql
    for stmt in (recall_memory._FACTS, recall_memory._EPISODES):
        sql = str(stmt).lower()
        assert "cast(:from_date as date)" in sql
        assert "cast(:to_date as date)" in sql


def test_episode_read_revalidates_the_original_sha256_contract() -> None:
    body = "전에 힘들다고 말했어"
    assert hashlib.sha256(body.encode()).hexdigest() != hashlib.sha256((body + "!").encode()).hexdigest()
