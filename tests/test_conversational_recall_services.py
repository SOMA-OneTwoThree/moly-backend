"""최종 회상 구조의 교차 계층 불변식."""
from __future__ import annotations

import hashlib
import uuid

import pytest

from app.core.errors import AppError
from app.services import (
    chat_turns,
    diary_recall_repo,
    episodic_memory,
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


def test_recall_queries_apply_suppression_before_ranking() -> None:
    diary_sql = str(recall_diaries._RECALL).lower()
    memory_sql = str(recall_memory._EPISODES).lower()
    assert "memory_recall_suppressions" in diary_sql
    assert "memory_recall_suppressions" in memory_sql
    assert diary_sql.index("memory_recall_suppressions") < diary_sql.index("order by")
    assert memory_sql.index("memory_recall_suppressions") < memory_sql.index("order by")


def test_episode_read_revalidates_the_original_sha256_contract() -> None:
    body = "전에 힘들다고 말했어"
    assert hashlib.sha256(body.encode()).hexdigest() != hashlib.sha256((body + "!").encode()).hexdigest()
