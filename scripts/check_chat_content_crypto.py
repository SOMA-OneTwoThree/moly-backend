"""Read-only deployment proof. Emits capability, IDs and a one-way key fingerprint only."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.content_crypto import initialize_content_crypto, seal, open_bytes  # noqa: E402
from app.core.db import get_sessionmaker, get_engine  # noqa: E402
from app.services.chat_content_encryption import load_mode  # noqa: E402

CAPABILITY = "chat-content-aead-v1"


async def check(environment: str, expected_sha: str):
    if (settings.environment != environment or settings.git_sha != expected_sha
            or not re.fullmatch(r"[0-9a-f]{40}", expected_sha)):
        raise ValueError("runtime_mismatch")
    async with get_sessionmaker()() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        mode = await load_mode(session)
        count = (await session.execute(text("""
          SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND
          ((table_name='messages' AND column_name IN ('content_ciphertext','content_chars')) OR
           (table_name='greetings' AND column_name='content_ciphertext') OR
           (table_name='idempotency_keys' AND column_name='response_ciphertext'))
        """))).scalar_one()
        if count != 4:
            raise ValueError("schema_mismatch")
    ring = initialize_content_crypto(required_key_ids=mode.required_key_ids)
    owner = uuid.UUID("00000000-0000-0000-0000-000000000001")
    fingerprints = {}
    for key_id in mode.required_key_ids:
        sample = "한영일 storage proof 日本語".encode()
        blob = seal(sample, user_id=owner, table="messages", field="content", key_id=key_id,
                    aad_extra=("content_chars", len(sample.decode())))
        if open_bytes(blob, user_id=owner, table="messages", field="content",
                      aad_extra=("content_chars", len(sample.decode()))) != sample:
            raise ValueError("self_test_failed")
        fingerprints[str(key_id)] = hashlib.sha256(ring._keys[key_id]).hexdigest()
    return {"capability": CAPABILITY, "environment": environment, "version": expected_sha,
            "started": mode.started, "format_version": mode.format_version,
            "active_key_id": mode.active_key_id, "key_fingerprints": fingerprints,
            "schema": "ok", "self_test": "ok"}


async def run(args):
    try:
        print(json.dumps(await check(args.environment, args.expected_sha), sort_keys=True))
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=("development", "production"))
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"capability": CAPABILITY, "status": "failed", "error": type(exc).__name__}))
        raise SystemExit(1) from None
