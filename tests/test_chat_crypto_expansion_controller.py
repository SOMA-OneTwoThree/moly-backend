"""Metadata-only bootstrap and bounded expansion; all connections are fake."""

from copy import deepcopy
import json
from unittest.mock import AsyncMock

import asyncpg
import pytest

from scripts import preflight_chat_content_encryption as p
from scripts import activate_chat_content_crypto as c
from tests.test_chat_crypto_release_controller import SHA, snapshot
from tests.test_content_crypto import ring
from app.core import content_crypto


class Transaction:
    def __init__(self, conn, kwargs):
        self.conn, self.kwargs = conn, kwargs

    async def __aenter__(self):
        self.conn.transactions.append(self.kwargs)
        return self

    async def __aexit__(self, kind, *args):
        self.conn.exits.append("rollback" if kind else "commit")
        return False


class Connection:
    def __init__(self, status="before", mode_missing=False):
        self.before, self.after = p.expected_catalogs()
        self.catalog = deepcopy(self.before if status == "before" else self.after)
        self.config = snapshot()
        if mode_missing:
            self.config[c.STATE_KEY] = {"exists": False, "value": None}
        self.transactions, self.exits, self.calls = [], [], []
        self.busy = 0
        self.drift_after = False
        self.closed = False

    def transaction(self, **kwargs):
        return Transaction(self, kwargs)

    async def execute(self, sql):
        self.calls.append(sql)
        if sql.startswith("LOCK") and self.busy:
            self.busy -= 1
            raise asyncpg.LockNotAvailableError("synthetic lock")
        if sql.startswith("DO "):
            self.catalog = deepcopy(self.after)
            if self.drift_after:
                self.catalog["columns"]["public.messages.content"]["not_null"] = True
            self.config = snapshot()

    async def fetchval(self, sql, *args):
        assert sql == p.CATALOG_SQL and args == (["public"],)
        return json.dumps(self.catalog)

    async def fetch(self, sql, *args):
        assert "FROM public.app_config" in sql
        return [
            {"key": k, "value": json.dumps(v["value"])}
            for k, v in self.config.items()
            if v["exists"]
        ]

    async def close(self):
        self.closed = True

    def terminate(self):
        self.closed = True


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    monkeypatch.setattr(p.settings, "environment", "development")
    monkeypatch.setattr(p.settings, "git_sha", SHA)
    monkeypatch.setattr(
        p.settings,
        "supabase_db_connection_string",
        "postgresql://postgres.wywzjslvxwttxkecbyis:x@aws-0-test.pooler.supabase.com/postgres",
    )
    monkeypatch.setattr(content_crypto, "_cached_keyring", ring("development"))


def options(**changes):
    return dict(
        environment="development",
        expected_project_ref=c.PROJECTS["development"],
        expected_sql_sha256=p.SQL_SHA256,
        expected_sha=SHA,
        **changes,
    )


def test_catalog_builder_never_selects_history_or_rows():
    assert "SELECT jsonb_build_object" in p.CATALOG_SQL
    assert "pg_get_constraintdef" in p.CATALOG_SQL and "pg_get_functiondef" in p.CATALOG_SQL
    assert "FROM public.messages" not in p.CATALOG_SQL
    assert "FROM public.greetings" not in p.CATALOG_SQL
    assert "FROM public.idempotency_keys" not in p.CATALOG_SQL
    assert "starts_with(q.key,'public.idempotency_keys.')" in p.CATALOG_SQL


def test_sql_body_checksum_and_transaction_control_are_fixed(tmp_path):
    sql = p.reviewed_body(p.SQL_PATH, p.SQL_SHA256)
    assert sql.startswith("DO $body_")
    assert "content_ciphertext bytea" in sql and "NOT VALID" in sql
    assert "SET LOCAL statement_timeout = '5s';" not in sql
    assert "\nCOMMIT;" not in sql and "\nBEGIN;" not in sql
    other = tmp_path / "unreviewed.sql"
    other.write_text(p.SQL_PATH.read_text() + "\n-- new input\n")
    with pytest.raises(c.ReleaseBlocked):
        p.reviewed_body(other, p.SQL_SHA256)
    with pytest.raises(c.ReleaseBlocked):
        p.reviewed_body(p.SQL_PATH, "0" * 64)


async def test_bootstrap_missing_columns_and_marker_is_readonly_needs_expansion(monkeypatch):
    conn = Connection(mode_missing=True)
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(p.asyncpg, "connect", connect)
    out = await p.run(**options())
    assert out["schema_status"] == "needs_expansion" and out["applied"] is False
    assert out["self_test"] == "ok" and set(out["key_fingerprints"]) == {"1"}
    assert out["config_snapshot"][c.STATE_KEY] == {"exists": False, "value": None}
    assert conn.transactions == [{"isolation": "repeatable_read", "readonly": True}]
    assert not any(sql.startswith(("DO ", "LOCK", "UPDATE ", "INSERT ")) for sql in conn.calls)
    assert conn.closed


async def test_expansion_uses_only_reviewed_ddl_after_nowait_and_commits(monkeypatch):
    conn = Connection(mode_missing=True)
    monkeypatch.setattr(p.asyncpg, "connect", AsyncMock(return_value=conn))
    out = await p.run(**options(apply=True))
    assert out["schema_status"] == "expanded" and out["applied"] is True
    locks = [s for s in conn.calls if s.startswith("LOCK")]
    assert len(locks) == 1 and "ACCESS EXCLUSIVE MODE NOWAIT" in locks[0]
    do = next(s for s in conn.calls if s.startswith("DO "))
    assert conn.calls.index(locks[0]) < conn.calls.index(do)
    assert conn.transactions[-1] == {"isolation": "read_committed"}
    assert conn.exits[-1] == "commit" and conn.closed


async def test_expanded_started_state_is_idempotent_no_public_change(monkeypatch):
    conn = Connection("expanded")
    conn.config[c.STATE_KEY]["value"]["started"] = True
    original = deepcopy(conn.config)
    monkeypatch.setattr(p.asyncpg, "connect", AsyncMock(return_value=conn))
    out = await p.run(**options(apply=True))
    assert out["schema_status"] == "expanded" and out["applied"] is False
    assert conn.config == original and not any(s.startswith("DO ") for s in conn.calls)


async def test_partial_schema_never_gets_repaired(monkeypatch):
    conn = Connection()
    conn.catalog["columns"]["public.messages.content_ciphertext"] = deepcopy(
        conn.after["columns"]["public.messages.content_ciphertext"]
    )
    monkeypatch.setattr(p.asyncpg, "connect", AsyncMock(return_value=conn))
    with pytest.raises(c.ReleaseBlocked, match="reviewed_schema_drift"):
        await p.run(**options(apply=True))
    assert conn.closed and not any(s.startswith("DO ") for s in conn.calls)


async def test_postcondition_failure_rolls_back_without_success():
    conn = Connection()
    conn.drift_after = True
    with pytest.raises(c.ReleaseBlocked, match="schema_postcondition_failed"):
        await p.apply_expansion(conn, expected_sha256=p.SQL_SHA256)
    assert conn.exits == ["rollback"]


async def test_only_contention_retries_three_times(monkeypatch):
    conn = Connection()
    conn.busy = 3
    sleep = AsyncMock()
    monkeypatch.setattr(p.asyncio, "sleep", sleep)
    with pytest.raises(asyncpg.LockNotAvailableError):
        await p.apply_expansion(conn, expected_sha256=p.SQL_SHA256)
    assert conn.exits == ["rollback"] * 3
    assert sleep.await_count == 2 and not any(s.startswith("DO ") for s in conn.calls)


async def test_wrong_environment_or_project_fails_before_connect(monkeypatch):
    connect = AsyncMock()
    monkeypatch.setattr(p.asyncpg, "connect", connect)
    with pytest.raises(c.ReleaseBlocked):
        await p.run(**{**options(), "expected_project_ref": c.PROJECTS["production"]})
    connect.assert_not_awaited()
