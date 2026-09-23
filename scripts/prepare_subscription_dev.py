"""Apply reviewed subscription prerequisites to the fixed development DB only.

One transaction; no product repricing, paid grants, campaign activation or production access.
Without --apply, perform the same catalog check and roll everything back.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.catalog import compare_catalog, read_catalog  # noqa: E402
from db.envfile import assert_dev_target, load_conn  # noqa: E402
from db.schema_contract import load_contract  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    "subscription_launch.sql", "subscription_offer_expiry.sql",
    "subscription_test_access.sql", "subscription_transfer.sql",
    "subscription_cleanup_acl.sql", "subscription_policy_safety.sql",
    "gpt6_price_catalog.sql",
)


async def run(env_file: str, apply: bool) -> None:
    dsn = load_conn(env_file)
    assert_dev_target(env_file, dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=60)
    tx = conn.transaction(isolation="repeatable_read")
    await tx.start()
    try:
        await conn.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='60s'")
        current = await conn.fetchval("SELECT value FROM public.app_config WHERE key='subscription_launch' FOR UPDATE")
        if current is not None and json.loads(current).get("enabled") is True:
            raise RuntimeError("Active dev rollout must not be overwritten")
        # Only the reviewed constraint change; preserve all dev product prices/ownership.
        cosmetic = (ROOT / "db/changes/subscriber_only_cosmetics.sql").read_text()
        constraint = re.search(r"ALTER TABLE public.products DROP CONSTRAINT products_cosmetic_ck;[\s\S]*?;", cosmetic)
        assert constraint is not None
        await conn.execute(constraint.group())
        for name in MIGRATIONS:
            sql = re.sub(r"^\s*(?:BEGIN|COMMIT);\s*$", "", (ROOT / "db/changes" / name).read_text(), flags=re.M | re.I)
            await conn.execute(sql)
        problems = compare_catalog(load_contract(), await read_catalog(conn), strict=False)
        if problems:
            raise RuntimeError("Prepared dev schema mismatch: " + "; ".join(problems[:15]))
        await conn.execute("""
            INSERT INTO public.app_config(key,value) VALUES('subscription_launch','{"enabled":false}'::jsonb)
            ON CONFLICT(key) DO UPDATE SET value=app_config.value || '{"enabled":false}'::jsonb,updated_at=now()
        """)
        if apply:
            await tx.commit()
        else:
            await tx.rollback()
        print(f"Development schema/catalog verified; rollout disabled; {'COMMITTED' if apply else 'ROLLED BACK'}")
    except BaseException:
        if conn.is_in_transaction():
            await tx.rollback()
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.env_file, args.apply))
