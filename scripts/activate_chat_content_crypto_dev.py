"""Final dev-only deployment gate: verify the fleet proof, then adopt atomically.

Called after the trusted infra drain and API/consumer/version/health checks. No
history conversion and no plaintext rollback. Input contains key hashes, not keys.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.exc import DBAPIError  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.db import get_sessionmaker, get_engine  # noqa: E402
from app.services.chat_content_encryption import activate  # noqa: E402
from db.envfile import DEV_PROJECT_REFS, project_ref  # noqa: E402
from scripts.check_chat_content_crypto import check  # noqa: E402


def validate_proofs(value, current):
    if not isinstance(value, dict) or set(value) != {"api", "consumer"}:
        raise ValueError("fleet_proof_invalid")
    for proof in value.values():
        if not isinstance(proof, dict) or set(proof) != set(current):
            raise ValueError("fleet_proof_invalid")
        # Adoption may already have completed in an earlier successful attempt.
        if type(proof.get("started")) is not bool or any(
            proof[key] != expected for key, expected in current.items() if key != "started"
        ):
            raise ValueError("fleet_proof_mismatch")


async def run(expected_sha, proof):
    if (settings.environment != "development"
            or project_ref(settings.supabase_db_connection_string) not in DEV_PROJECT_REFS):
        raise ValueError("development_target_required")
    current = await check("development", expected_sha)
    validate_proofs(proof, current)
    for attempt in range(3):
        try:
            async with get_sessionmaker()() as session:
                mode = await activate(session, expected_key_id=current["active_key_id"],
                    expected_required_key_ids=tuple(int(key) for key in current["key_fingerprints"]),
                    release_sha=expected_sha)
                await session.commit()
            return {"environment": "development", "version": expected_sha, "started": mode.started}
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != "55P03" or attempt == 2:
                raise
            await asyncio.sleep(0.1 * (attempt + 1))


async def execute(expected_sha, proof):
    try:
        print(json.dumps(await run(expected_sha, proof)))
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError("fleet_proof_too_large")
        asyncio.run(execute(args.expected_sha, json.loads(raw)))
    except Exception as exc:
        print(json.dumps({"environment": "development", "status": "blocked", "error": type(exc).__name__}))
        raise SystemExit(1) from None
