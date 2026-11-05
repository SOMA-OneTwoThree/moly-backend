"""Local synthetic codec integration: no database, provider, or model calls."""
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
import uuid

import pytest

from app.core import content_crypto
from app.services import (checkpoint_repo, context_provenance, context_retention,
                          diary_recall_repo, jobs, message_content, provided_memory,
                          retention_release_promotion as promotion)
from scripts import verify_idempotency_responses as verifier
from tests.test_content_crypto import ring
from tests.test_idempotency_response_verifier import VALID, FakeConnection
from worker import contract_jobs

OWNER = uuid.UUID(int=1)
MODE = SimpleNamespace(started=True, active_key_id=1, format_version=1, required_key_ids=(1,))
TEXT = "한글🙂 日本語\nquote \\\"와 두 번째 문장"


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(content_crypto, "_cached_keyring", ring())


def storage(text=TEXT):
    return message_content.encode_message(user_id=OWNER, text=text, mode=MODE)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


async def test_checkpoint_mixed_formats_keep_logical_sources(monkeypatch):
    now = datetime.now(timezone.utc)
    encrypted = storage()
    rows = [dict(id=1, sender="user", kind="normal", content=TEXT, created_at=now),
            dict(id=2, sender="moly", kind="normal", created_at=now, **encrypted)]

    async def execute(*args):
        return Rows(rows)

    monkeypatch.setattr(checkpoint_repo.retention_release, "execute", execute)
    result = await checkpoint_repo.load_range(object(), OWNER, after_id=0, through_id=2)
    assert [row.content for row in result] == [TEXT, TEXT]
    assert rows[1]["content"] is None  # RAM projection never dirties the stored row.
    rows[1]["content_chars"] += 1
    with pytest.raises(message_content.ContentCryptoError):
        await checkpoint_repo.load_range(object(), OWNER, after_id=0, through_id=2)


@pytest.mark.parametrize("value", ["", TEXT, "x" * 3000])
def test_raw_orm_source_hash_and_budget_equal_plaintext(value):
    plain = SimpleNamespace(id=8, user_id=OWNER, content=value)
    encrypted = SimpleNamespace(id=8, user_id=OWNER, **storage(value))
    assert context_provenance.SourceRef.from_message(encrypted, user_id=OWNER, generation=3) == (
        context_provenance.SourceRef.from_message(plain, user_id=OWNER, generation=3))
    assert context_retention.source_tokens([encrypted]) == context_retention.source_tokens([plain])
    assert encrypted.content is None
    with pytest.raises(message_content.ContentCryptoError):
        context_provenance.SourceRef.from_message(encrypted, user_id=uuid.UUID(int=2), generation=3)


def source_row():
    body = provided_memory.memory.sanitize_text(TEXT).encode()
    return dict(actual_message_id=9, source_message_id=9, message_kind="normal",
                message_sender="user", source_sender="user", authority="explicit_user",
                start_utf8=0, end_utf8=len(body), span_hash=hashlib.sha256(body).hexdigest(),
                source_occurred_at=datetime.now(timezone.utc), message_content=TEXT)


def test_optional_memory_proof_hashes_decoded_original_not_ciphertext():
    plain = source_row()
    encoded = storage()
    row = {**plain, "message_content": None, "message_ciphertext": encoded["content_ciphertext"],
           "message_chars": encoded["content_chars"]}
    assert provided_memory._source(row, user_id=OWNER) == provided_memory._source(plain, user_id=OWNER)
    assert provided_memory._source(row, user_id=OWNER).raw_content_hash == hashlib.sha256(TEXT.encode()).hexdigest()
    row["span_hash"] = "0" * 64
    with pytest.raises(provided_memory.ProvidedMemoryError):
        provided_memory._source(row, user_id=OWNER)


@pytest.mark.parametrize("width", [3, 4])
def test_contract_source_projection_preserves_original_tuple_shape(width):
    encoded = storage()
    suffix = () if width == 3 else (datetime.now(timezone.utc).date(),)
    raw = [(7, "user", None, *suffix, encoded["content_ciphertext"], encoded["content_chars"])]
    assert contract_jobs._logical_source_rows(raw, OWNER, width) == [(7, "user", TEXT, *suffix)]
    assert raw[0][2] is None


async def test_diary_source_records_only_original_plaintext_hash():
    rows = [dict(id=3, sender="user", **storage())]
    calls = []

    class Session:
        async def execute(self, sql, params):
            calls.append((sql, params))
            return Rows(rows)

    await diary_recall_repo.record_diary_sources(Session(), user_id=OWNER, diary_id=uuid.UUID(int=4), message_ids=[3])
    assert calls[1][1]["source_hash"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert set(calls[1][1]) == {"user_id", "diary_id", "message_id", "source_hash"}


def receipt(text=TEXT):
    encrypted = storage(text)
    return dict(user_id=OWNER, id=4, sender="user", kind="normal", content=None,
                content_ciphertext_hex=encrypted["content_ciphertext"].hex(),
                content_chars=encrypted["content_chars"])


def test_preparation_cipher_receipt_has_identical_logical_tail_hash():
    row = receipt()
    result = promotion._logical_message(row, receipt=True)
    plain = dict(user_id=OWNER, id=4, sender="user", kind="normal", content=TEXT)
    assert result == plain
    assert promotion.tail_digest([result]) == promotion.tail_digest([plain])
    assert row["content"] is None and "content_ciphertext_hex" in row
    # Legacy text resembling a bytea prefix stays literal plaintext.
    assert promotion._logical_message({**plain, "content": "\\x1234"}, receipt=True)["content"] == "\\x1234"


@pytest.mark.parametrize("bad", ["0", "g" * 64, "AA" * 32, " " + "ab" * 32, "ab" * 31])
def test_preparation_receipt_rejects_noncanonical_hex(bad):
    row = receipt()
    row["content_ciphertext_hex"] = bad
    with pytest.raises(message_content.ContentCryptoError):
        promotion._logical_message(row, receipt=True)


def response_row(response=VALID):
    encoded = message_content.encode_response(user_id=OWNER, key="request", request_hash="hash", response=response, mode=MODE)
    return dict(user_id=str(OWNER), key="request", request_hash="hash", redacted_at=None,
                terminal_status="succeeded", **encoded)


def test_verifier_preserves_encrypted_responses_and_tombstones():
    row = response_row()
    assert verifier.classify_response_row(row) == "compatible"
    assert verifier.classify_response_row(response_row({"reply": {}})) == "protected"
    assert verifier.classify_response_row({**row, "response_ciphertext": None}) == "protected"
    assert verifier.classify_response_row({**row, "response": None, "response_ciphertext": None,
                                          "redacted_at": datetime.now(timezone.utc)}) == "tombstone"
    assert verifier.classify_response_row({**row, "user_id": str(uuid.UUID(int=2))}) == "protected"


async def test_verifier_delete_invalid_never_deletes_unverifiable_cipher(monkeypatch):
    connection = FakeConnection()
    connection.rows.append({**response_row(), "response_ciphertext": None})

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(verifier, "_connection_string", lambda: "postgresql://synthetic")
    monkeypatch.setattr(verifier.asyncpg, "connect", connect)
    assert await verifier.run(delete_invalid=True) > 0
    assert connection.deleted is None and connection.closed


def test_retention_scrub_clears_both_cache_storage_forms_without_index_changes():
    sql = str(jobs._SCRUB_RETENTION_SQL)
    assert "response=NULL,response_ciphertext=NULL,redacted_at=now()" in sql
    assert "response IS NOT NULL" in sql


@pytest.mark.parametrize("entrypoint", ["consumer", "tick"])
async def test_worker_reader_key_check_fails_before_probe_or_background_tasks(monkeypatch, entrypoint):
    from app.core import db
    from app.services import chat_content_encryption
    from worker import consumer
    import worker.__main__ as entry

    events = []

    class Session:
        async def __aenter__(self):
            events.append("session")
            return self

        async def __aexit__(self, *args):
            events.append("closed")

    async def prepare(session):
        events.append("keys")
        raise message_content.ContentCryptoError("content_key_missing")

    def forbidden(*args, **kwargs):
        raise AssertionError("worker started before required key validation")

    monkeypatch.setattr(db, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(consumer, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(chat_content_encryption, "prepare_reader", prepare)
    monkeypatch.setattr(entry.synthetic_probe, "start", forbidden)
    monkeypatch.setattr(consumer, "_register_handlers", forbidden)
    with pytest.raises(message_content.ContentCryptoError):
        if entrypoint == "consumer":
            await consumer.run_consumer(queues=())
        else:
            await entry._tick_then_flush()
    assert events == ["session", "keys", "closed"]


async def test_manual_contract_backfill_reads_cipher_but_keeps_stored_source_cas(monkeypatch):
    from scripts import backfill_interaction_contracts as backfill
    encoded = storage()
    rows = [(1, "user", None, encoded["content_ciphertext"], encoded["content_chars"])]
    opened = False

    class Session:
        async def __aenter__(self):
            nonlocal opened
            opened = True
            return self

        async def __aexit__(self, *args):
            nonlocal opened
            opened = False

        async def execute(self, sql, params):
            if sql is backfill._MESSAGES:
                return Rows(rows)
            return SimpleNamespace(first=lambda: ("synthetic", "ko"))

    async def authorize(*args):
        return 3

    async def generate(system, messages, **kwargs):
        assert not opened
        assert TEXT in messages[0]["content"]
        return SimpleNamespace(text="[]")

    monkeypatch.setattr(backfill, "lock_active_subject", authorize)
    monkeypatch.setattr(backfill.llm, "generate", generate)
    result = await backfill._compile_for_user(Session, OWNER)
    assert result[-1] == rows and result[-1][0][2] is None


def test_raw_orm_missing_cipher_never_becomes_empty_logical_source():
    corrupted = SimpleNamespace(id=8, user_id=OWNER, content=None,
                                content_ciphertext=None, content_chars=4)
    with pytest.raises(message_content.ContentCryptoError):
        context_provenance.SourceRef.from_message(corrupted, user_id=OWNER, generation=3)
    with pytest.raises(message_content.ContentCryptoError):
        context_retention.source_tokens([corrupted])
