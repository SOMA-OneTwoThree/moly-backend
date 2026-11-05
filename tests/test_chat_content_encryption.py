"""Publish and replay encrypt every owned conversation copy without altering output."""
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import content_crypto
from app.models.message import Message
from app.models.idempotency_key import IdempotencyKey
from app.services import chat, crisis_guard
from app.services.chat_content_encryption import EncryptionMode
from tests.test_guard_context_v2_integration_review import setup as guard_setup_fixture, Session, request
from tests.test_chat import UID

setup = guard_setup_fixture


@pytest.fixture
def encrypted(monkeypatch):
    ring = content_crypto.parse_keyring(json.dumps({"environment": "development", "keys": {
        "1": base64.b64encode(b"k" * 32).decode()}}), environment="development")
    monkeypatch.setattr(content_crypto, "_cached_keyring", ring)
    monkeypatch.setattr(chat.chat_turns, "verify_publish", AsyncMock(
        return_value=EncryptionMode(True, 1, 1, (1,))))


@pytest.mark.parametrize("route,kind", [("normal", "normal"), ("ask", "safety_check"), ("safety", "safety")])
async def test_published_messages_and_replay_have_no_plaintext_storage(setup, encrypted, monkeypatch, route, kind):
    monkeypatch.setattr(chat.crisis_chat, "recorded_decision", AsyncMock(return_value=crisis_guard.DecisionResult(
        "ok", route=route, case="self_thought")))
    session = Session()
    req = request("합성 암호화 검증")
    output = await chat.post_message(session, UID, req, "synthetic-encrypted")
    rows = [row for row in session.added if isinstance(row, Message)]
    assert len(rows) == 2
    assert all(row.content is None and row.content_ciphertext and row.kind == kind for row in rows)
    assert chat._message_text(rows[0]) == req.text
    assert chat._message_text(rows[1]) == output.reply.content
    snapshot = next(row for row in session.added if isinstance(row, IdempotencyKey))
    assert snapshot.response_schema_version == 3 and snapshot.response == {} and snapshot.response_ciphertext
    assert chat.validate_post_message_response(chat._replay_payload(snapshot)) == output
    replay_session = Session()
    replay_session.get_map["IdempotencyKey"] = snapshot
    replayed = await chat.post_message(replay_session, UID, req, "synthetic-encrypted")
    assert replayed == output and not replay_session.added


async def test_corrupt_replay_never_regenerates_or_removes_dedupe(setup, encrypted, monkeypatch):
    from app.services.message_content import encode_response
    mode = EncryptionMode(True, 1, 1, (1,))
    encoded = encode_response(user_id=UID, key="synthetic-corrupt", request_hash="digest",
                              response={"synthetic": True}, mode=mode)
    row = SimpleNamespace(user_id=UID, key="synthetic-corrupt", request_hash=None,
                          terminal_status="succeeded", response_expires_at=None, redacted_at=None, **encoded)
    row.response_ciphertext = row.response_ciphertext[:-1] + bytes([row.response_ciphertext[-1] ^ 1])
    session = Session()
    session.get_map["IdempotencyKey"] = row
    with pytest.raises(content_crypto.ContentCryptoError):
        await chat.post_message(session, UID, request(), "synthetic-corrupt")
    assert not session.added and not session.deleted
    chat.llm.generate.assert_not_awaited()
