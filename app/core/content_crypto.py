"""Local, versioned authenticated content envelopes. No network or database access."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field as dataclass_field
import json
import os
import re
from types import MappingProxyType
from typing import Mapping
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

FORMAT_VERSION = 1
OVERHEAD = 32
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ContentCryptoError(Exception):
    """Only a fixed code escapes: never key material, payloads or driver text."""
    def __init__(self, code: str = "content_crypto_unavailable"):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "content_crypto_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class Keyring:
    environment: str
    _keys: Mapping[int, bytes] = dataclass_field(repr=False)

    @property
    def key_ids(self) -> frozenset[int]:
        return frozenset(self._keys)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ContentCryptoError("content_keyring_invalid")
        value[key] = item
    return value


def parse_keyring(raw: SecretStr | str, *, environment: str) -> Keyring:
    try:
        if isinstance(raw, SecretStr):
            raw = raw.get_secret_value()
        if not isinstance(raw, str) or not isinstance(environment, str) or not environment:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_pairs)
        if not isinstance(value, dict) or set(value) != {"environment", "keys"} or value["environment"] != environment:
            raise ValueError
        keys = value["keys"]
        if not isinstance(keys, dict) or not keys:
            raise ValueError
        decoded = {}
        for number, encoded in keys.items():
            if not isinstance(number, str) or not re.fullmatch(r"[1-9][0-9]{0,4}", number) or int(number) > 65535:
                raise ValueError
            if not isinstance(encoded, str):
                raise ValueError
            key = base64.b64decode(encoded, validate=True)
            if len(key) != 32 or base64.b64encode(key).decode("ascii") != encoded or key in decoded.values():
                raise ValueError
            decoded[int(number)] = key
        return Keyring(environment, MappingProxyType(decoded))
    except Exception:
        raise ContentCryptoError("content_keyring_invalid") from None


_UNSET = object()
_cached_keyring: Keyring | None | object = _UNSET


def initialize_content_crypto(*, required_key_ids=()) -> Keyring | None:
    """Parse the injected secret once per process; a required missing key fails closed."""
    global _cached_keyring
    if _cached_keyring is _UNSET:
        from app.config import settings
        secret = settings.chat_content_encryption_keys
        _cached_keyring = parse_keyring(secret, environment=settings.environment) if secret is not None else None
    try:
        ids = tuple(required_key_ids)
        if any(type(key) is not int or not 1 <= key <= 65535 for key in ids):
            raise ValueError
    except Exception:
        raise ContentCryptoError("content_required_keys_invalid") from None
    ring = _cached_keyring
    if ids and (ring is None or not set(ids).issubset(ring.key_ids)):
        raise ContentCryptoError("content_key_unavailable")
    return ring


def _ring(keyring):
    ring = initialize_content_crypto() if keyring is None else keyring
    if not isinstance(ring, Keyring):
        raise ContentCryptoError("content_key_unavailable")
    return ring


def _aad(*, ring, user_id, table, field, header, aad_extra):
    try:
        owner = str(uuid.UUID(str(user_id)))
        if (table, field) not in {("messages", "content"), ("greetings", "content"), ("idempotency_keys", "response")}:
            raise ValueError
        return json.dumps(["moly-content", ring.environment, owner, table, field, header.hex(), aad_extra],
                          ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except Exception:
        raise ContentCryptoError("content_aad_invalid") from None


def seal(plaintext: bytes, *, user_id, table: str, field: str, key_id: int,
         aad_extra=(), max_plaintext_bytes: int = MAX_MESSAGE_BYTES, keyring: Keyring | None = None) -> bytes:
    if not isinstance(plaintext, bytes) or len(plaintext) > max_plaintext_bytes:
        raise ContentCryptoError("content_size_invalid")
    ring = _ring(keyring)
    if type(key_id) is not int or key_id not in ring.key_ids:
        raise ContentCryptoError("content_key_unavailable")
    header = bytes((FORMAT_VERSION, 0)) + key_id.to_bytes(2, "big")
    aad = _aad(ring=ring, user_id=user_id, table=table, field=field, header=header, aad_extra=aad_extra)
    try:
        nonce = os.urandom(12)
        return header + nonce + AESGCM(ring._keys[key_id]).encrypt(nonce, plaintext, aad)
    except Exception:
        raise ContentCryptoError("content_encryption_failed") from None


def open_bytes(ciphertext, *, user_id, table: str, field: str, aad_extra=(),
               max_plaintext_bytes: int = MAX_MESSAGE_BYTES, keyring: Keyring | None = None) -> bytes:
    if not isinstance(ciphertext, (bytes, bytearray, memoryview)):
        raise ContentCryptoError("content_ciphertext_invalid")
    size = ciphertext.nbytes if isinstance(ciphertext, memoryview) else len(ciphertext)
    if not OVERHEAD <= size <= max_plaintext_bytes + OVERHEAD:
        raise ContentCryptoError("content_size_invalid")
    data = bytes(ciphertext)
    header = data[:4]
    key_id = int.from_bytes(header[2:4], "big")
    if header[:2] != bytes((FORMAT_VERSION, 0)) or key_id == 0:
        raise ContentCryptoError("content_format_unsupported")
    ring = _ring(keyring)
    if key_id not in ring.key_ids:
        raise ContentCryptoError("content_key_unavailable")
    aad = _aad(ring=ring, user_id=user_id, table=table, field=field, header=header, aad_extra=aad_extra)
    try:
        plain = AESGCM(ring._keys[key_id]).decrypt(data[4:16], data[16:], aad)
    except Exception:
        raise ContentCryptoError("content_authentication_failed") from None
    if len(plain) != len(data) - OVERHEAD:
        raise ContentCryptoError("content_size_invalid")
    return plain
