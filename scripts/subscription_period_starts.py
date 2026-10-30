"""Preview/apply store period starts without exporting account or transaction data."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from db.envfile import load_conn, project_ref  # noqa: E402

PROPOSALS = """
WITH evidence AS (
 SELECT s.id,s.user_id,s.latest_transaction_id,s.expires_at,s.last_event_at,
 CASE WHEN e.payload->>'purchased_at_ms'~'^[0-9]+$'
 THEN to_timestamp((e.payload->>'purchased_at_ms')::numeric/1000) END period_start,
 CASE WHEN e.payload->>'expiration_at_ms'~'^[0-9]+$'
 THEN to_timestamp((e.payload->>'expiration_at_ms')::numeric/1000) END period_end
 FROM subscriptions s JOIN revenuecat_events e
 ON e.payload->>'original_transaction_id'=s.original_transaction_id
 AND e.payload->>'transaction_id'=s.latest_transaction_id
 AND e.payload->>'app_user_id'=s.user_id::text
 AND e.payload->>'environment'=s.environment
 WHERE s.purchased_at IS NULL AND s.ownership_changed_at IS NULL AND s.environment='PRODUCTION'
 AND s.status IN ('active','grace_period','expired') AND e.status='processed'
 AND e.payload->>'type' IN ('INITIAL_PURCHASE','RENEWAL')
 AND ($1::uuid IS NULL OR s.id=$1)
)
SELECT id,user_id,latest_transaction_id,expires_at,last_event_at,min(period_start) period_start
FROM evidence WHERE period_start<period_end AND period_end<=expires_at
GROUP BY id,user_id,latest_transaction_id,expires_at,last_event_at
HAVING count(DISTINCT period_start)=1 ORDER BY id
"""


def plan_hash(rows, ref: str) -> str:
    value = {"project": ref, "rows": [dict(row) for row in rows]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     separators=(",", ":")).encode()).hexdigest()


async def run(args) -> dict:
    dsn = load_conn(args.env)
    ref = project_ref(dsn)
    if ref not in {"wywzjslvxwttxkecbyis", "qkgjlgzsharnilxnkytd"}:
        raise ValueError("unsupported database target")
    if args.commit and ref == "qkgjlgzsharnilxnkytd" and not args.allow_prod:
        raise ValueError("explicit production target required")
    connection = await asyncpg.connect(dsn, timeout=8, command_timeout=10, statement_cache_size=0)
    try:
        async with connection.transaction(readonly=True):
            rows = await connection.fetch(PROPOSALS, None)
        digest = plan_hash(rows, ref)
        result = {"candidates": len(rows), "sha256": digest, "updated": 0, "skipped": 0}
        if not args.commit:
            return result
        if not args.expected_sha256 or args.expected_sha256 != digest:
            raise ValueError("preview SHA-256 required")
        for row in rows:
            try:
                async with connection.transaction():
                    await connection.execute("SET LOCAL lock_timeout='1s'")
                    lock = await connection.fetchval(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))", str(row["user_id"]),
                    )
                    if not lock:
                        result["skipped"] += 1
                        continue
                    await connection.fetchval("SELECT id FROM subscriptions WHERE id=$1 FOR UPDATE NOWAIT", row["id"])
                    current = await connection.fetch(PROPOSALS, row["id"])
                    if len(current) != 1 or dict(current[0]) != dict(row):
                        result["skipped"] += 1
                        continue
                    changed = await connection.fetchval(
                        "UPDATE subscriptions SET purchased_at=$2 WHERE id=$1 AND purchased_at IS NULL RETURNING 1",
                        row["id"], row["period_start"],
                    )
                    result["updated"] += int(changed == 1)
            except asyncpg.LockNotAvailableError:
                result["skipped"] += 1
        return result
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--allow-prod", action="store_true")
    parser.add_argument("--expected-sha256")
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args()))))
    except Exception as exc:
        raise SystemExit(type(exc).__name__) from None


if __name__ == "__main__":
    main()
