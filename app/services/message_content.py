"""Plaintext read projections and storage values; decoded values never dirty ORM rows."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

from app.core import content_crypto as crypto
from app.models.idempotency_key import RESERVED_KEY_PREFIXES

ContentCryptoError = crypto.ContentCryptoError


def _mode(mode):
    try:
        started, active, version, required = mode.started, mode.active_key_id, mode.format_version, tuple(mode.required_key_ids)
        if (type(started) is not bool or type(version) is not int or version != 1
                or type(active) is not int or not 1 <= active <= 65535
                or any(type(key) is not int or not 1 <= key <= 65535 for key in required)
                or len(required) != len(set(required)) or active not in required):
            raise ValueError
    except Exception:
        raise ContentCryptoError("content_mode_invalid") from None
    if started:
        crypto.initialize_content_crypto(required_key_ids=required)
    return started, active


def _encode_text(text):
    if not isinstance(text, str):
        raise ContentCryptoError("content_text_invalid")
    try:
        return text.encode("utf-8")
    except UnicodeError:
        raise ContentCryptoError("content_utf8_invalid") from None


def _decode_text(data):
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise ContentCryptoError("content_utf8_invalid") from None


def encode_message(*, user_id, text: str, mode) -> dict:
    started, active = _mode(mode)
    if not isinstance(text, str):
        raise ContentCryptoError("content_text_invalid")
    if not started:
        return {"content": text, "content_ciphertext": None, "content_chars": None}
    chars = len(text)
    blob = crypto.seal(_encode_text(text), user_id=user_id, table="messages", field="content",
                       key_id=active, aad_extra=("content_chars", chars))
    return {"content": None, "content_ciphertext": blob, "content_chars": chars}


def read_message_content(*, user_id, ciphertext=None, legacy_text=None, chars=None) -> str:
    if ciphertext is None:
        if not isinstance(legacy_text, str) or chars is not None:
            raise ContentCryptoError("content_storage_shape_invalid")
        return legacy_text
    if legacy_text is not None or type(chars) is not int or chars < 0:
        raise ContentCryptoError("content_storage_shape_invalid")
    value = _decode_text(crypto.open_bytes(ciphertext, user_id=user_id, table="messages", field="content",
                                           aad_extra=("content_chars", chars)))
    if len(value) != chars:
        raise ContentCryptoError("content_chars_invalid")
    return value


def encode_greeting(*, user_id, text: str, mode) -> dict:
    started, active = _mode(mode)
    if not isinstance(text, str):
        raise ContentCryptoError("content_text_invalid")
    return {"content": None if started else text,
            "content_ciphertext": crypto.seal(_encode_text(text), user_id=user_id, table="greetings",
                field="content", key_id=active) if started else None}


def read_greeting_content(*, user_id, ciphertext=None, legacy_text=None) -> str:
    if ciphertext is None:
        if not isinstance(legacy_text, str):
            raise ContentCryptoError("content_storage_shape_invalid")
        return legacy_text
    if legacy_text is not None:
        raise ContentCryptoError("content_storage_shape_invalid")
    return _decode_text(crypto.open_bytes(ciphertext, user_id=user_id, table="greetings", field="content"))


read_greeting = read_greeting_content


def _response_aad(key, request_hash, version):
    if (not isinstance(key, str) or not key or key.startswith(RESERVED_KEY_PREFIXES)
            or not isinstance(request_hash, str) or not request_hash or type(version) is not int or version != 3):
        raise ContentCryptoError("content_response_identity_invalid")
    return ("key", key, "schema_version", version, "request_hash", request_hash)


def encode_response(*, user_id, key, request_hash, response, mode, version=2) -> dict:
    started, active = _mode(mode)
    if not started:
        if type(version) is not int or version not in (1, 2):
            raise ContentCryptoError("content_response_version_invalid")
        return {"response_schema_version": version, "response": response, "response_ciphertext": None}
    if not isinstance(response, dict):
        raise ContentCryptoError("content_response_shape_invalid")
    try:
        data = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise ContentCryptoError("content_response_shape_invalid") from None
    blob = crypto.seal(data, user_id=user_id, table="idempotency_keys", field="response", key_id=active,
        aad_extra=_response_aad(key, request_hash, 3), max_plaintext_bytes=crypto.MAX_RESPONSE_BYTES)
    return {"response_schema_version": 3, "response": {}, "response_ciphertext": blob}


def read_response(*, user_id, key, request_hash, version, response, ciphertext,
                  redacted_at=None, terminal_status="succeeded"):
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            raise ContentCryptoError("content_response_shape_invalid") from None
    if type(version) is not int:
        raise ContentCryptoError("content_response_version_invalid")
    if version in (1, 2):
        if ciphertext is not None:
            raise ContentCryptoError("content_response_shape_invalid")
        return response
    if version != 3:
        raise ContentCryptoError("content_response_version_invalid")
    if response is None and ciphertext is None:
        if redacted_at is not None or terminal_status in {"expired", "redacted"}:
            return None
        raise ContentCryptoError("content_response_cipher_missing")
    if not isinstance(response, dict) or response or ciphertext is None or redacted_at is not None or terminal_status != "succeeded":
        raise ContentCryptoError("content_response_shape_invalid")
    data = crypto.open_bytes(ciphertext, user_id=user_id, table="idempotency_keys", field="response",
        aad_extra=_response_aad(key, request_hash, version), max_plaintext_bytes=crypto.MAX_RESPONSE_BYTES)
    try:
        value = json.loads(_decode_text(data))
    except ValueError:
        raise ContentCryptoError("content_response_shape_invalid") from None
    if not isinstance(value, dict):
        raise ContentCryptoError("content_response_shape_invalid")
    return value


def project_message(row, *, user_id=None, text_key="content", ciphertext_key="content_ciphertext", chars_key="content_chars") -> dict:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    if not isinstance(mapping, Mapping):
        raise ContentCryptoError("content_projection_invalid")
    result = dict(mapping)
    owner = result.get("user_id") if user_id is None else user_id
    if owner is None:
        raise ContentCryptoError("content_projection_invalid")
    result[text_key] = read_message_content(user_id=owner, legacy_text=result.get(text_key),
        ciphertext=result.get(ciphertext_key), chars=result.get(chars_key))
    return result


def _alias(alias):
    if not isinstance(alias, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*", alias):
        raise ContentCryptoError("content_sql_alias_invalid")
    return alias


def sql_content_chars(alias="m") -> str:
    alias = _alias(alias)
    return f"CASE WHEN {alias}.content_ciphertext IS NULL THEN char_length({alias}.content) ELSE {alias}.content_chars END"


def sql_content_bytes(alias="m") -> str:
    alias = _alias(alias)
    return f"CASE WHEN {alias}.content_ciphertext IS NULL THEN octet_length({alias}.content) ELSE octet_length({alias}.content_ciphertext)-32 END"
