"""Storage adapters never dirty mapped plaintext columns or trust malformed v3."""
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.core import content_crypto as c
from app.services import message_content as m
from tests.test_content_crypto import ring

UID = uuid.UUID(int=1)


@pytest.fixture(autouse=True)
def isolated_ring(monkeypatch):
    monkeypatch.setattr(c, "_cached_keyring", ring())


def mode(started=True):
    return SimpleNamespace(started=started, active_key_id=1, format_version=1, required_key_ids=(1,))


def test_message_dual_read_and_ram_projection():
    body = "이름 <name>🙂\n再試行"
    fields = m.encode_message(user_id=UID, text=body, mode=mode())
    assert fields["content"] is None and fields["content_chars"] == len(body)
    assert len(fields["content_ciphertext"]) == len(body.encode()) + 32
    row = {"user_id": UID, "id": 12, **fields}
    projected = m.project_message(row)
    assert projected["content"] == body and row["content"] is None
    assert projected["id"] == 12
    assert m.read_message_content(user_id=UID, legacy_text=body) == body
    assert m.encode_message(user_id=UID, text=body, mode=mode(False))["content"] == body


@pytest.mark.parametrize("changes", [{"chars": True}, {"chars": -1}, {"chars": 100}, {"legacy_text": "duplicate"}])
def test_message_authenticated_chars_and_exclusive_storage(changes):
    fields = m.encode_message(user_id=UID, text="🙂", mode=mode())
    args = dict(user_id=UID, ciphertext=fields["content_ciphertext"], chars=1, legacy_text=None)
    args.update(changes)
    with pytest.raises(c.ContentCryptoError):
        m.read_message_content(**args)


@pytest.mark.parametrize("legacy,chars", [(None, None), ("text", 4)])
def test_legacy_message_shape_is_unambiguous(legacy, chars):
    with pytest.raises(c.ContentCryptoError):
        m.read_message_content(user_id=UID, legacy_text=legacy, chars=chars)


def test_greeting_same_owner_but_cross_table_cipher_rejected():
    fields = m.encode_greeting(user_id=UID, text="안녕🙂", mode=mode())
    assert fields["content"] is None
    assert m.read_greeting_content(user_id=UID, ciphertext=fields["content_ciphertext"]) == "안녕🙂"
    with pytest.raises(c.ContentCryptoError):
        m.read_message_content(user_id=UID, ciphertext=fields["content_ciphertext"], chars=3)


def response_fields():
    return m.encode_response(user_id=UID, key="retry-key", request_hash="h" * 64,
        response={"reply": "synthetic🙂", "diary_cards": [{"id": "one"}]}, mode=mode())


def read_response(fields, **changes):
    args = dict(user_id=UID, key="retry-key", request_hash="h" * 64,
        version=fields["response_schema_version"], response=fields["response"], ciphertext=fields["response_ciphertext"])
    args.update(changes)
    return m.read_response(**args)


def test_v3_exact_sentinel_and_response_identity():
    fields = response_fields()
    assert fields["response"] == {} and fields["response_schema_version"] == 3
    assert read_response(fields)["reply"] == "synthetic🙂"
    for changes in [dict(key="other"), dict(request_hash="other"), dict(user_id=uuid.UUID(int=2))]:
        with pytest.raises(c.ContentCryptoError):
            read_response(fields, **changes)


@pytest.mark.parametrize("changes", [dict(response=None), dict(response=[]), dict(response="null"), dict(response={"copy": "bad"}),
    dict(ciphertext=None), dict(redacted_at=datetime.now(timezone.utc)), dict(terminal_status="expired"), dict(version=True), dict(version=4)])
def test_v3_corruption_not_a_valid_replay(changes):
    with pytest.raises(c.ContentCryptoError):
        read_response(response_fields(), **changes)


def test_v3_sql_null_scrub_only_with_explicit_lifecycle():
    fields = response_fields()
    assert read_response(fields, response=None, ciphertext=None, redacted_at=datetime.now(timezone.utc)) is None
    assert read_response(fields, response=None, ciphertext=None, terminal_status="redacted") is None
    with pytest.raises(c.ContentCryptoError):
        read_response(fields, response=None, ciphertext=None)


def test_v1_v2_and_nonchat_namespace_unchanged():
    value = {"legacy": "shape"}
    for version in (1, 2):
        fields = m.encode_response(user_id=UID, key="shop-purchase:1", request_hash=None,
            response=value, mode=mode(False), version=version)
        assert fields["response"] is value and read_response(fields, key="shop-purchase:1", request_hash=None) is value
    with pytest.raises(c.ContentCryptoError):
        m.encode_response(user_id=UID, key="shop-purchase:1", request_hash="hash", response=value, mode=mode())


def test_plaintext_mode_needs_no_secret_but_encryption_mode_requires_it(monkeypatch):
    monkeypatch.setattr(c, "_cached_keyring", None)
    assert m.encode_message(user_id=UID, text="legacy", mode=mode(False))["content"] == "legacy"
    with pytest.raises(c.ContentCryptoError):
        m.encode_message(user_id=UID, text="new", mode=mode())
    with pytest.raises(c.ContentCryptoError):
        m.encode_message(user_id=UID, text="new", mode=None)


def test_invalid_utf8_never_becomes_empty_source():
    blob = c.seal(b"\xff", user_id=UID, table="messages", field="content", key_id=1, aad_extra=("content_chars", 1))
    with pytest.raises(c.ContentCryptoError, match="content_utf8_invalid"):
        m.read_message_content(user_id=UID, ciphertext=blob, chars=1)
    with pytest.raises(c.ContentCryptoError):
        m.encode_message(user_id=UID, text="\ud800", mode=mode())


def test_sql_lengths_use_plain_utf8_and_safe_alias():
    assert "char_length(m.content)" in m.sql_content_chars()
    assert "octet_length(alias.content_ciphertext)-32" in m.sql_content_bytes("alias")
    for alias in ["m;DROP", "", "x.y", None]:
        with pytest.raises(c.ContentCryptoError):
            m.sql_content_bytes(alias)
