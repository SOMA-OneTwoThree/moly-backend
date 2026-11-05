"""One database policy and transaction fence for newly stored conversation bodies.

Key material is process-local. The database contains only non-secret key identifiers.
The marker is seeded by expansion SQL, so FOR SHARE locks an existing row even before
adoption. An activator takes only that row, never a user/context lock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.content_crypto import ContentCryptoError, initialize_content_crypto

STATE_KEY = "chat_content_encryption_state"


@dataclass(frozen=True)
class EncryptionMode:
    started: bool
    active_key_id: int
    format_version: int
    required_key_ids: tuple[int, ...]


def parse_mode(value: object) -> EncryptionMode:
    if not isinstance(value, dict) or type(value.get("started")) is not bool:
        raise ContentCryptoError("encryption_state_invalid")
    active = value.get("active_key_id")
    required = value.get("required_key_ids")
    if (type(active) is not int or not 1 <= active <= 65535
            or type(value.get("format_version")) is not int or value["format_version"] != 1
            or not isinstance(required, list) or not required
            or any(type(key) is not int or not 1 <= key <= 65535 for key in required)
            or len(set(required)) != len(required) or active not in required):
        raise ContentCryptoError("encryption_state_invalid")
    return EncryptionMode(value["started"], active, 1, tuple(required))


async def load_mode(session, *, lock: bool = False) -> EncryptionMode:
    suffix = " FOR SHARE" if lock else ""
    value = (await session.execute(text(
        "SELECT value FROM app_config WHERE key=:encryption_key" + suffix
    ), {"encryption_key": STATE_KEY})).scalar_one_or_none()
    mode = parse_mode(value)
    if mode.started:
        initialize_content_crypto(required_key_ids=mode.required_key_ids)
    return mode


async def prepare_reader(session) -> EncryptionMode:
    """Startup/preflight: reject missing required keys before accepting traffic."""
    mode = await load_mode(session)
    initialize_content_crypto(required_key_ids=mode.required_key_ids if mode.started else ())
    return mode


async def prepare_runtime():
    from app.core.db import get_sessionmaker
    async with get_sessionmaker()() as session:
        return await prepare_reader(session)


async def activate(session, *, expected_key_id: int, expected_required_key_ids: tuple[int, ...],
                   release_sha: str) -> EncryptionMode:
    """Called only after the deployment controller proves all readers compatible."""
    if len(release_sha) != 40 or any(c not in "0123456789abcdef" for c in release_sha):
        raise ContentCryptoError("release_sha_invalid")
    value = (await session.execute(text(
        "SELECT value FROM app_config WHERE key=:key FOR UPDATE NOWAIT"
    ), {"key": STATE_KEY})).scalar_one_or_none()
    mode = parse_mode(value)
    if (type(expected_key_id) is not int or mode.active_key_id != expected_key_id
            or not isinstance(expected_required_key_ids, tuple)
            or any(type(key) is not int for key in expected_required_key_ids)
            or sorted(mode.required_key_ids) != sorted(expected_required_key_ids)):
        raise ContentCryptoError("encryption_key_changed")
    initialize_content_crypto(required_key_ids=mode.required_key_ids)
    if not mode.started:
        updated = {**value, "started": True, "activated_at": datetime.now(timezone.utc).isoformat(),
                   "release_sha": release_sha}
        import json
        await session.execute(text(
            "UPDATE app_config SET value=CAST(:value AS jsonb),updated_at=now() WHERE key=:key"
        ), {"key": STATE_KEY, "value": json.dumps(updated)})
    return EncryptionMode(True, mode.active_key_id, 1, mode.required_key_ids)
