"""Opt-in deployed-development reader verification; never sends a chat or webhook.

MOLY_SUBSCRIPTION_REFUND_API_TEST_ENV=dev \
MOLY_SUBSCRIPTION_REFUND_API_EXPECTED_SHA=<40-character deployed development SHA> \
MOLY_SUBSCRIPTION_REFUND_ENV_FILE=/absolute/path/to/development.env \
python -m pytest tests/integration/test_subscription_refund_api_dev.py -q

The three random Auth users and their SANDBOX fixtures are deleted in finally.
The accelerated trial/paid windows test reader boundaries, not real billing duration.
No global configuration, provider state, messages, or financial records are written.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest
from dotenv import dotenv_values

from db.envfile import assert_dev_target, load_conn

_API = "https://dev.moly.asia"
_AUTH = "https://wywzjslvxwttxkecbyis.supabase.co"
_CONFIG_KEYS = ["subscription_access_evidence_enabled", "subscription_access_evidence_users"]


class _CheckFailed(Exception):
    """Only static labels may be used, so failures cannot disclose account data."""


def _require(condition: bool, label: str) -> None:
    if not condition:
        raise _CheckFailed(label)


def _instant(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _evidence(uid: uuid.UUID, now: datetime, boundary: datetime, end: datetime, *, empty=False) -> dict:
    rc_id = f"synthetic-reader-{uuid.uuid4().hex}"
    trial_tx, paid_tx = f"synthetic-trial-{uuid.uuid4().hex}", f"synthetic-paid-{uuid.uuid4().hex}"

    def period(tx, start, finish, plan, kind):
        return {
            "transaction_id": tx, "rc_subscription_id": rc_id, "owner_id": str(uid),
            "store": "app_store", "environment": "SANDBOX", "start": start.isoformat(),
            "end": finish.isoformat(), "owner_start": None, "owner_end": None,
            "plan": plan, "kind": kind, "source": "synthetic_development_reader_fixture",
        }

    return {
        "version": 1, "revision": 1, "verified_at": now.isoformat(),
        "rc_subscription_id": rc_id, "owner_id": str(uid), "store": "app_store",
        "environment": "SANDBOX", "auto_renew_enabled": False,
        "current_transaction_id": trial_tx, "legacy_history": [],
        # Empty JSON periods means no verified access; SQL NULL would mean legacy.
        "periods": [] if empty else [
            period(trial_tx, now - timedelta(days=2), boundary, "monthly", "trial"),
            period(paid_tx, boundary, end, "yearly", "paid"),
        ],
    }


async def test_deployed_subscription_readers_sticky_off_and_time_boundaries():
    if os.getenv("MOLY_SUBSCRIPTION_REFUND_API_TEST_ENV") != "dev":
        pytest.skip("explicit development subscription API opt-in required")

    # All target checks precede every connection and mutation. No application engine is used.
    expected_sha = os.getenv("MOLY_SUBSCRIPTION_REFUND_API_EXPECTED_SHA", "")
    env_path = os.getenv("MOLY_SUBSCRIPTION_REFUND_ENV_FILE", "")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        pytest.fail("full expected deployed development SHA required", pytrace=False)
    if not env_path or not Path(env_path).is_absolute():
        pytest.fail("explicit absolute development env file required", pytrace=False)
    try:
        dsn = load_conn(env_path)
        assert_dev_target(env_path, dsn)
        env = dotenv_values(env_path, interpolate=False)
        _require((env.get("SUPABASE_URL") or "").rstrip("/") == _AUTH, "development Auth target required")
        secret = env.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_SERVICE_ROLE_KEY")
        public = env.get("SUPABASE_PUBLISHABLE_KEY") or env.get("SUPABASE_ANON_KEY")
        _require(bool(secret and public), "development Auth credentials required")
    except (Exception, SystemExit):
        pytest.fail("development target/configuration validation failed", pytrace=False)

    admin = {"apikey": secret, "Authorization": f"Bearer {secret}"}
    users: list[uuid.UUID] = []
    emails: list[str] = []
    db = None
    failure = None
    cleanup_errors: list[str] = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        try:
            async def health():
                response = await client.get(f"{_API}/health")
                _require(response.status_code == 200, "development health HTTP failure")
                payload = response.json()
                _require(payload.get("env") == "development" and payload.get("version") == expected_sha,
                         "development health/SHA mismatch")

            await health()
            db = await asyncpg.connect(dsn, statement_cache_size=0, timeout=15, command_timeout=15)

            async def config_snapshot():
                rows = await db.fetch("SELECT key,value::text,updated_at FROM app_config WHERE key=ANY($1::text[])",
                                      _CONFIG_KEYS)
                return {row["key"]: (row["value"], row["updated_at"]) for row in rows}

            saved_config = await config_snapshot()
            enabled = json.loads(saved_config[_CONFIG_KEYS[0]][0]) if _CONFIG_KEYS[0] in saved_config else False
            _require(enabled is False, "first-adoption flag must already be off; test never changes it")
            clients = {}
            for case, language in (("boundary", "ko"), ("legacy", "en"), ("refunded", "ja")):
                email = f"subscription-reader-{uuid.uuid4().hex}@moly.test"
                emails.append(email)  # Recover the exact synthetic UUID if creation times out after commit.
                response = await client.post(f"{_AUTH}/auth/v1/admin/users", headers=admin,
                    json={"email": email, "password": uuid.uuid4().hex, "email_confirm": True})
                _require(response.status_code in (200, 201), "synthetic Auth creation failed")
                uid = uuid.UUID(response.json()["id"])
                owns_email = await db.fetchval("SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=$1 AND email=$2)",
                                               uid, email)
                _require(owns_email, "synthetic Auth identity verification failed")
                users.append(uid)
                response = await client.post(f"{_AUTH}/auth/v1/admin/generate_link", headers=admin,
                                             json={"type": "magiclink", "email": email})
                _require(response.status_code == 200, "synthetic Auth link failed")
                response = await client.post(f"{_AUTH}/auth/v1/verify", headers={"apikey": public},
                    json={"type": "magiclink", "token_hash": response.json()["hashed_token"]})
                _require(response.status_code == 200, "synthetic authentication failed")
                clients[case] = (uid, language, response.json()["access_token"])

            now = await db.fetchval("SELECT clock_timestamp()")
            boundary, paid_end = now + timedelta(seconds=20), now + timedelta(seconds=45)
            legacy_end, stale_end = now + timedelta(days=30), now + timedelta(days=365)
            async with db.transaction():
                for case, (uid, language, _) in clients.items():
                    result = await db.execute("""UPDATE profiles SET nickname='API fixture',language=$2,
                        timezone='UTC',created_at=$3::timestamptz-interval '30 days',trial_ends_at=NULL,
                        app_trial_started_at=$3::timestamptz-interval '4 days',
                        app_trial_ends_at=$3::timestamptz-interval '2 days',
                        next_diary_due_at=NULL WHERE id=$1""", uid, language, now)
                    _require(result == "UPDATE 1", "synthetic profile bootstrap missing")
                    await db.execute("""INSERT INTO user_notification_settings(user_id,type,enabled)
                        VALUES($1,'morning_diary',false),($1,'evening_chat',false)
                        ON CONFLICT(user_id,type) DO UPDATE SET enabled=false""", uid)
                    evidence = None if case == "legacy" else _evidence(
                        uid, now, boundary, paid_end, empty=case == "refunded")
                    await db.execute("""INSERT INTO subscriptions(user_id,plan,status,original_transaction_id,
                        latest_transaction_id,purchased_at,expires_at,store_trial_ends_at,auto_renew_enabled,
                        environment,access_evidence,rc_subscription_id)
                        VALUES($1,$2,'active',$3,$4,$5,$6,$7,false,'SANDBOX',$8::jsonb,$9)""",
                        uid, "monthly" if case == "legacy" else "yearly",
                        f"synthetic-original-{uuid.uuid4().hex}", f"synthetic-flat-{uuid.uuid4().hex}",
                        now - timedelta(days=2), legacy_end if case == "legacy" else stale_end,
                        None if case == "legacy" else stale_end, json.dumps(evidence) if evidence else None,
                        evidence["rc_subscription_id"] if evidence else None)

            fixture_rows = await db.fetchval("""SELECT jsonb_agg(to_jsonb(s) ORDER BY s.id)::text
                FROM subscriptions s WHERE user_id=ANY($1::uuid[])""", users)

            async def probe(case, status, plan, trial, limit, expiry=None):
                _, _, token = clients[case]
                headers = {"Authorization": f"Bearer {token}"}
                response = await client.get(f"{_API}/subscription", headers=headers)
                _require(response.status_code == 200, f"{case}: subscription HTTP failure")
                payload = response.json()
                _require(payload.get("status") == status and payload.get("in_trial") is trial,
                         f"{case}: subscription status/trial mismatch")
                if expiry is not None:
                    _require(_instant(payload.get("expires_at")) == expiry,
                             f"{case}: current period expiration mismatch")
                _require((_instant(payload.get("trial_ends_at")) == boundary) if trial else
                         payload.get("trial_ends_at") is None, f"{case}: trial end mismatch")
                if status == "active":
                    _require(payload.get("plan") == plan, f"{case}: subscription plan mismatch")
                response = await client.get(f"{_API}/chat/state", headers=headers)
                _require(response.status_code == 200, f"{case}: chat state HTTP failure")
                state = response.json()
                _require(state.get("plan") == plan and state.get("daily_token_limit") == limit,
                         f"{case}: common reader plan/quota mismatch")
                _require(state.get("tokens_used") == 0 and state.get("tokens_remaining") == limit
                         and state.get("limit_reached") is False, f"{case}: unused quota mismatch")

            async def controls():
                await probe("legacy", "active", "monthly", False, 300_000, legacy_end)
                # An adopted empty period list must override stale active/yearly/trial flat fields.
                await probe("refunded", "expired", "free", False, 45_000)

            await probe("boundary", "active", "monthly", True, 400_000, boundary)
            await controls()
            _require(await db.fetchval("SELECT clock_timestamp()") < boundary,
                     "initial API probes crossed the trial boundary; rerun required")

            async def wait_until(target):
                deadline = time.monotonic() + 60
                while await db.fetchval("SELECT clock_timestamp()") < target:
                    _require(time.monotonic() < deadline, "bounded development boundary wait expired")
                    await asyncio.sleep(0.25)

            # No writes, reconciliation, or webhook occur between any of these phases.
            await wait_until(boundary + timedelta(seconds=2))
            await probe("boundary", "active", "yearly", False, 400_000, paid_end)
            await controls()
            _require(await db.fetchval("SELECT clock_timestamp()") < paid_end,
                     "paid API probes crossed the paid boundary; rerun required")
            await wait_until(paid_end + timedelta(seconds=2))
            await probe("boundary", "expired", "free", False, 40_000)
            await controls()

            after_rows = await db.fetchval("""SELECT jsonb_agg(to_jsonb(s) ORDER BY s.id)::text
                FROM subscriptions s WHERE user_id=ANY($1::uuid[])""", users)
            _require(after_rows == fixture_rows, "reader unexpectedly mutated subscription fixture")
            _require(await config_snapshot() == saved_config, "adoption configuration changed during verification")
            for table in ("messages", "ai_usage_ledger", "payments", "hay_transactions", "async_jobs"):
                # Table names are a fixed source-code allowlist; values are only the created UUIDs.
                count = await db.fetchval(f"SELECT count(*) FROM {table} WHERE user_id=ANY($1::uuid[])", users)
                _require(count == 0, f"unexpected synthetic {table} side effect")
            await health()
        except Exception as exc:
            failure = str(exc) if isinstance(exc, _CheckFailed) else f"sanitized {type(exc).__name__}"
        finally:
            if db is not None:
                try:
                    # Exact random emails only; covers an Auth create response lost after commit.
                    recovered = await db.fetch("SELECT id FROM auth.users WHERE email=ANY($1::text[])", emails)
                    users = list(dict.fromkeys([*users, *(row["id"] for row in recovered)]))
                except Exception:
                    cleanup_errors.append("synthetic Auth recovery failed")
                for uid in users:
                    try:
                        response = await client.delete(f"{_AUTH}/auth/v1/admin/users/{uid}", headers=admin)
                        if response.status_code not in (200, 204, 404):
                            cleanup_errors.append("synthetic Auth deletion HTTP failure")
                    except Exception:
                        cleanup_errors.append("synthetic Auth deletion transport failure")
                try:
                    # If the admin transport failed, finish only these exact synthetic Auth rows.
                    # The UUID AND original random email guard prevents unrelated-row deletion.
                    await db.execute("DELETE FROM auth.users WHERE id=ANY($1::uuid[]) AND email=ANY($2::text[])",
                                     users, emails)
                    # Auth deletion cascades all these rows. Never touch any untracked UUID.
                    for table, column in (("auth.users", "id"), ("profiles", "id"),
                                          ("subscriptions", "user_id"), ("messages", "user_id"),
                                          ("user_notification_settings", "user_id"), ("async_jobs", "user_id")):
                        count = await db.fetchval(f"SELECT count(*) FROM {table} WHERE {column}=ANY($1::uuid[])", users)
                        if count:
                            cleanup_errors.append("synthetic cascade cleanup incomplete")
                except Exception:
                    cleanup_errors.append("synthetic cleanup verification failed")
                finally:
                    try:
                        await db.close(timeout=10)
                    except Exception:
                        cleanup_errors.append("development connection close failed")
    if failure or cleanup_errors:
        pytest.fail("; ".join(filter(None, [failure, *cleanup_errors])), pytrace=False)
