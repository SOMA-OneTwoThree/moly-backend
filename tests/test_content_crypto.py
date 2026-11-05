"""Authenticated local codec: synthetic keys/content only, no provider or database."""
import base64
import json
import uuid
from dataclasses import replace

import pytest
from pydantic import SecretStr

from app.core import content_crypto as c


OWNER = uuid.UUID(int=1)


def ring(environment="test", keys=None):
    return c.parse_keyring(SecretStr(json.dumps({"environment": environment, "keys": keys or {
        "1": base64.b64encode(b"a" * 32).decode(), "2": base64.b64encode(b"b" * 32).decode()}})), environment=environment)


@pytest.fixture(autouse=True)
def isolated_ring(monkeypatch):
    monkeypatch.setattr(c, "_cached_keyring", ring())


def enc(value=b"synthetic", **kwargs):
    return c.seal(value, user_id=OWNER, table="messages", field="content", key_id=1, **kwargs)


def dec(value, **kwargs):
    return c.open_bytes(value, user_id=OWNER, table="messages", field="content", **kwargs)


@pytest.mark.parametrize("value", [b"", b"a", "한글🙂\n日本語".encode(), b"x" * (1024 * 1024)])
def test_exact_overhead_and_nonce(value):
    a, b = enc(value), enc(value)
    assert len(a) == len(value) + 32 and a[:4] == b"\x01\x00\x00\x01"
    assert a != b and dec(a) == value and dec(memoryview(a)) == value


@pytest.mark.parametrize("position", [0, 1, 2, 3, 4, 15, 16, -1])
def test_all_envelope_parts_authenticated(position):
    value = bytearray(enc())
    value[position] ^= 1
    with pytest.raises(c.ContentCryptoError):
        dec(value)


def test_owner_environment_table_and_metadata_authenticated():
    blob = enc(aad_extra=("chars", 9))
    for values in [dict(user_id=uuid.UUID(int=2), table="messages", field="content"),
                   dict(user_id=OWNER, table="greetings", field="content"),
                   dict(user_id=OWNER, table="messages", field="content", aad_extra=("chars", 8)),
                   dict(user_id=OWNER, table="messages", field="content", keyring=replace(ring(), environment="other"))]:
        values.setdefault("aad_extra", ("chars", 9))
        with pytest.raises(c.ContentCryptoError):
            c.open_bytes(blob, **values)


@pytest.mark.parametrize("value", [b"", b"x" * 31, b"x" * (1024 * 1024 + 33), None, "not bytes"])
def test_bad_size_and_type_rejected(value):
    with pytest.raises(c.ContentCryptoError):
        dec(value)


def test_size_caps_fail_before_crypto():
    with pytest.raises(c.ContentCryptoError, match="content_size_invalid"):
        enc(b"x" * (1024 * 1024 + 1))
    with pytest.raises(c.ContentCryptoError):
        dec(enc(b"abcd"), max_plaintext_bytes=3)


@pytest.mark.parametrize("raw", ["", "null", "{}", '{"environment":"test","environment":"test","keys":{}}',
    '{"environment":"other","keys":{}}', '{"environment":"test","keys":{"0":"x"}}',
    '{"environment":"test","keys":{"01":"x"}}', '{"environment":"test","keys":{"65536":"x"}}',
    '{"environment":"test","keys":{"1":"not base64"}}',
    json.dumps({"environment": "test", "keys": {"1": base64.b64encode(b"a" * 31).decode()}}),
    json.dumps({"environment": "test", "keys": {"1": base64.b64encode(b"a" * 32).decode(), "2": base64.b64encode(b"a" * 32).decode()}})])
def test_malformed_keyrings_do_not_expose_input(raw):
    with pytest.raises(c.ContentCryptoError) as e:
        c.parse_keyring(raw, environment="test")
    assert str(e.value) == "content_keyring_invalid" and e.value.__suppress_context__


def test_old_keys_remain_readable_and_missing_key_fails_closed(monkeypatch):
    blob = enc()
    assert c.seal(b"new", user_id=OWNER, table="messages", field="content", key_id=2)
    assert dec(blob) == b"synthetic"
    monkeypatch.setattr(c, "_cached_keyring", None)
    with pytest.raises(c.ContentCryptoError, match="content_key_unavailable"):
        dec(blob)
    with pytest.raises(c.ContentCryptoError):
        c.initialize_content_crypto(required_key_ids=[1])


def test_keyring_repr_does_not_reveal_keys():
    assert "aaaa" not in repr(ring())
    assert "SecretStr" in repr(SecretStr("sensitive")) and "sensitive" not in repr(SecretStr("sensitive"))
