"""Strict policy validation and final-write fencing, independent of model/provider calls."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.core.content_crypto import ContentCryptoError
from app.services import chat_content_encryption as policy
from app.services.chat_turns import verify_publish as actual_verify, Lease

STATE = {"started": False, "active_key_id": 1, "format_version": 1, "required_key_ids": [1]}


@pytest.mark.parametrize("key,value", [
    ("started", None), ("started", 1), ("active_key_id", True), ("active_key_id", 0),
    ("active_key_id", 65536), ("format_version", True), ("format_version", 2),
    ("required_key_ids", []), ("required_key_ids", [True]), ("required_key_ids", [1, 1]),
    ("required_key_ids", [2]), ("required_key_ids", "1"),
])
def test_policy_cannot_coerce_invalid_database_state(key, value):
    state = {**deepcopy(STATE), key: value}
    with pytest.raises(ContentCryptoError):
        policy.parse_mode(state)


async def test_final_verify_returns_policy_in_one_locked_query():
    query = []

    async def execute(statement, args):
        query.append(str(statement))
        return SimpleNamespace(first=lambda: (1, 0, STATE))

    mode = await actual_verify(SimpleNamespace(execute=execute), user_id=uuid.uuid4(),
                               lease=Lease(uuid.uuid4(), 1, 0), encryption_mode=True)
    assert mode == policy.EncryptionMode(False, 1, 1, (1,))
    assert len(query) == 1 and "FOR SHARE" in query[0] and "FOR UPDATE OF a,c" in query[0]


async def test_missing_policy_never_means_plaintext_fallback():
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    with pytest.raises(ContentCryptoError):
        await policy.load_mode(session, lock=True)


@pytest.mark.parametrize("started", [False, True])
async def test_activation_only_changes_false_to_true_once(monkeypatch, started):
    state = {**STATE, "started": started}
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: state)))
    keys = []
    monkeypatch.setattr(policy, "initialize_content_crypto", lambda **kw: keys.append(kw))
    result = await policy.activate(session, expected_key_id=1, expected_required_key_ids=(1,), release_sha="a" * 40)
    assert result.started and keys == [{"required_key_ids": (1,)}]
    assert session.execute.await_count == (1 if started else 2)
    if not started:
        import json
        assert json.loads(session.execute.await_args_list[1].args[1]["value"])["started"] is True


async def test_activation_rejects_key_change_before_any_write():
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: STATE)))
    with pytest.raises(ContentCryptoError):
        await policy.activate(session, expected_key_id=2, expected_required_key_ids=(1,), release_sha="a" * 40)
    assert session.execute.await_count == 1


async def test_required_key_change_between_proof_and_lock_cannot_activate():
    state = {**STATE, "required_key_ids": [1, 2]}
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: state)))
    with pytest.raises(ContentCryptoError, match="encryption_key_changed"):
        await policy.activate(session, expected_key_id=1, expected_required_key_ids=(1,), release_sha="a" * 40)
    assert session.execute.await_count == 1
