"""Independent release-contract checks. Every connection is mocked; no DB is used."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from db import envfile, schema_contract as contract

_COL = {"type": "jsonb", "not_null": False, "default": None,
        "identity": "", "generated": "", "collation": None}
_KEYS = {"public.subscriptions.access_evidence", "public.revenuecat_events.processing_state"}
_DEV_DSN = (
    "postgresql://postgres.wywzjslvxwttxkecbyis:synthetic"
    "@aws-0-ap-northeast-2.pooler.supabase.com:6543/postgres"
)
# Reviewed production main df2a2b7c baseline. These hashes bind the entire source
# and catalog, including daily_practice, grants and message kinds. Do not derive
# them from an unreviewed current checkout or a mutable remote branch during CI.
_BASELINE_SOURCE_SHA256 = "4f7be7843ebed85b34ff4be939fd53eae8fe3ea1f129a17d713ddcf535da2999"
_BASELINE_CATALOG_SHA256 = "59d59beb9a85f422555a2ebeca1b0a90c98cf6b80db28429e4a412d03da70220"


def test_release_catalog_preserves_main_except_two_columns_and_source_hash():
    after = json.loads(contract.CONTRACT.read_bytes())
    restored_catalog = deepcopy(after["catalog"])
    for key in _KEYS:
        assert restored_catalog["columns"].pop(key) == _COL
    serialized_catalog = json.dumps(restored_catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert hashlib.sha256(serialized_catalog.encode()).hexdigest() == _BASELINE_CATALOG_SHA256
    assert after["schema_sha256"] == hashlib.sha256(contract.SCHEMA.read_bytes()).hexdigest()
    main_source = contract.SCHEMA.read_text()
    for added, original in (
        ("    payload jsonb NOT NULL,\n    processing_state jsonb,\n", "    payload jsonb NOT NULL,\n"),
        ("    last_event_at timestamp with time zone,\n    access_evidence jsonb,\n",
         "    last_event_at timestamp with time zone,\n"),
    ):
        assert main_source.count(added) == 1
        main_source = main_source.replace(added, original, 1)
    assert hashlib.sha256(main_source.encode()).hexdigest() == _BASELINE_SOURCE_SHA256
    assert contract.subscription_schema_upgrade(main_source) == contract.SCHEMA.read_text()
    assert "public.daily_practice_offers" in after["catalog"]["tables"]
    assert "public.daily_practice_templates" in after["catalog"]["tables"]
    assert contract.load_contract() == after["catalog"]


@pytest.fixture
def release(tmp_path, monkeypatch):
    # Use small, isolated source/catalog fixtures to test regeneration failure paths.
    baseline = (
        "CREATE TABLE public.revenuecat_events (\n    event_id text NOT NULL,\n    payload jsonb NOT NULL,\n);\n"
        "CREATE TABLE public.subscriptions (\n    last_event_at timestamp with time zone,\n"
        "    CONSTRAINT subscriptions_plan_check CHECK (true)\n);\n"
    )
    source, output, sql, prior = (tmp_path / name for name in ("schema.sql", "contract.json", "release.sql", "baseline.sql"))
    prior.write_text(baseline)
    source.write_text(contract.subscription_schema_upgrade(baseline))
    sql.write_text("ALTER TABLE public.subscriptions ADD COLUMN access_evidence jsonb;\n")
    catalog = {
        "columns": {"public.daily_practice_templates.title_ko": {"type": "text"}},
        "tables": {"public.daily_practice_templates": {"owner": "postgres"}},
        "constraints": {"public.messages.messages_kind_check": {"definition": "legacy kinds"}},
        "relation_grants": {"main-owned-grant": {"grantable": False}},
    }
    original = {"schema_sha256": hashlib.sha256(prior.read_bytes()).hexdigest(), "catalog": catalog}
    output.write_text(json.dumps(original))
    monkeypatch.setattr(contract, "SCHEMA", source)
    monkeypatch.setattr(contract, "CONTRACT", output)
    monkeypatch.setattr(contract, "SUBSCRIPTION_SQL", sql)
    monkeypatch.setattr(envfile, "load_conn", lambda _: _DEV_DSN)
    connection = AsyncMock()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(contract.asyncpg, "connect", connect)
    # Simulate a dev catalog missing main's daily_practice objects and containing
    # an unrelated dev-only column. Neither difference may enter the release.
    actual = {"columns": {key: deepcopy(_COL) for key in _KEYS}, "tables": {}}
    actual["columns"]["public.chat_contexts.contract_generation"] = {"type": "bigint"}
    reader = AsyncMock(return_value=actual)
    monkeypatch.setattr(contract, "read_catalog", reader)
    kwargs = {"env": "dev", "expected_project_ref": "wywzjslvxwttxkecbyis", "reviewed_sql": sql,
              "expected_sha256": hashlib.sha256(sql.read_bytes()).hexdigest(), "baseline_schema": prior}
    return kwargs, original, actual, connect, connection, reader


async def test_refresh_uses_only_reviewed_dev_definitions_and_preserves_main(release):
    kwargs, original, _, connect, connection, reader = release
    assert await contract.refresh_subscription_from_dev(**kwargs) == []
    expected = deepcopy(original["catalog"])
    expected["columns"].update({key: _COL for key in _KEYS})
    generated = json.loads(contract.CONTRACT.read_bytes())
    assert generated["catalog"] == expected
    assert generated["schema_sha256"] == hashlib.sha256(contract.SCHEMA.read_bytes()).hexdigest()
    connect.assert_awaited_once()
    reader.assert_awaited_once_with(connection)
    connection.close.assert_awaited_once()
    connection.execute.assert_not_called()


@pytest.mark.parametrize("change", ["env", "project", "dsn_override", "sql_path", "sql_hash", "baseline_hash", "source_drift"])
async def test_refresh_rejects_wrong_inputs_before_any_connection(release, monkeypatch, tmp_path, change):
    kwargs, _, _, connect, _, _ = release
    before = contract.CONTRACT.read_bytes()
    if change == "env":
        kwargs["env"] = "prod"
    elif change == "project":
        kwargs["expected_project_ref"] = "unconfirmed"
    elif change == "dsn_override":
        monkeypatch.setattr(envfile, "load_conn", lambda _: _DEV_DSN + "?host=unconfirmed.invalid")
    elif change == "sql_path":
        kwargs["reviewed_sql"] = tmp_path / "unreviewed.sql"
    elif change == "sql_hash":
        kwargs["expected_sha256"] = "0" * 64
    elif change == "baseline_hash":
        kwargs["baseline_schema"].write_text("unreviewed baseline")
    else:
        contract.SCHEMA.write_text(contract.SCHEMA.read_text() + "-- unrelated change\n")
    with pytest.raises((ValueError, SystemExit)):
        await contract.refresh_subscription_from_dev(**kwargs)
    connect.assert_not_awaited()
    assert contract.CONTRACT.read_bytes() == before


@pytest.mark.parametrize("dsn", [
    _DEV_DSN.replace("aws-0-ap-northeast-2.pooler.supabase.com", "db.invalid"),
    _DEV_DSN.replace(":6543/", ":443/"),
    _DEV_DSN.rsplit("/", 1)[0] + "/different_database",
    _DEV_DSN.replace("aws-0-ap-northeast-2.pooler.supabase.com", "db.unconfirmed.supabase.co"),
])
async def test_refresh_rejects_unconfirmed_endpoint_before_connecting(release, monkeypatch, dsn):
    kwargs, _, _, connect, _, _ = release
    before = contract.CONTRACT.read_bytes()
    monkeypatch.setattr(envfile, "load_conn", lambda _: dsn)
    with pytest.raises(ValueError, match="connection endpoint"):
        await contract.refresh_subscription_from_dev(**kwargs)
    connect.assert_not_awaited()
    assert contract.CONTRACT.read_bytes() == before


@pytest.mark.parametrize("field,value", [("type", "text"), ("not_null", True), ("default", "'{}'::jsonb")])
async def test_refresh_refuses_live_column_definition_drift(release, field, value):
    kwargs, _, actual, _, connection, _ = release
    before = contract.CONTRACT.read_bytes()
    actual["columns"]["public.subscriptions.access_evidence"][field] = value
    assert await contract.refresh_subscription_from_dev(**kwargs)
    assert contract.CONTRACT.read_bytes() == before
    connection.close.assert_awaited_once()


@pytest.mark.parametrize("changed", ["source", "baseline", "sql", "contract"])
async def test_refresh_detects_input_change_during_catalog_read(release, monkeypatch, changed):
    kwargs, _, actual, _, connection, _ = release
    original_output = contract.CONTRACT.read_bytes()
    target = {"source": contract.SCHEMA, "baseline": kwargs["baseline_schema"],
              "sql": kwargs["reviewed_sql"], "contract": contract.CONTRACT}[changed]

    async def raced(_):
        target.write_bytes(target.read_bytes() + b"\n")
        return actual

    monkeypatch.setattr(contract, "read_catalog", raced)
    with pytest.raises(ValueError, match="inputs changed"):
        await contract.refresh_subscription_from_dev(**kwargs)
    expected_output = original_output + (b"\n" if changed == "contract" else b"")
    assert contract.CONTRACT.read_bytes() == expected_output
    connection.close.assert_awaited_once()
    assert not list(contract.CONTRACT.parent.glob(".subscription-contract-*"))
