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
    # Independent frozen oracle: main64 plus ONLY the reviewed guard/context
    # extension. Do not call the implementation's upgrade/manifest for this check.
    expected_main_catalog = "446d522a19e3f2930e58a56740adedb7726cd68926f0eb1a9b4984697480fe96"
    expected_main_source = "e5929c8dc95a01842822ff94be432d8df791244ffdbf91820310d3f94a403e7a"
    expected_guard_source = "38a58491013c7a985dc66647127eb3a33daca3a94be4faa1c84c989b5dcb177f"
    allowed = {
        "columns": {
            "public.chat_contexts.contract_generation": "477f2bf1d5266fef2bbb4ff76b497bd75ed4f82caf70001405888cc0317673ef",
            "public.chat_contexts.contract_processed_through_message_id": "477f2bf1d5266fef2bbb4ff76b497bd75ed4f82caf70001405888cc0317673ef",
            "public.chat_contexts.retention_pressure_through_message_id": "232e6e5aadd392dd7e510da531a0cde5c6489722e441801e4ce4b991f4bb8ec0",
            "public.conversation_checkpoints.state_json": "29ca0049a1d41e390b32730f026a6768138373d79b28e11ab0452a999dfccabd",
            "public.conversation_checkpoints.validation_json": "29ca0049a1d41e390b32730f026a6768138373d79b28e11ab0452a999dfccabd",
            "public.mem0_ingest_candidates.context_priority": "cfe2121ba7cf2e1d24d5c906ddc112a2d07e1fab1f78f24a4f68a067247fce17",
            "public.mem0_memory_registry.canonical_text": "1bd779a1c25846c9b0ce5450401cde94c967d432d903c3a5d9b79a6372926d58",
            "public.mem0_memory_registry.context_priority": "cfe2121ba7cf2e1d24d5c906ddc112a2d07e1fab1f78f24a4f68a067247fce17",
        },
        "constraints": {
            "public.chat_contexts.chat_contexts_retention_pressure_positive_check": "213f732d0736c3bc54827ff5cbb3c754985ef71190ecbafb96e2ac813a3f1f34",
            "public.mem0_ingest_candidates.mem0_ingest_candidates_context_priority_check": "cb5676625458ab9f319d05edb7896c348da90d01bb94f998900eb2545b316b0f",
            "public.mem0_memory_registry.mem0_memory_registry_context_priority_check": "cb5676625458ab9f319d05edb7896c348da90d01bb94f998900eb2545b316b0f",
        },
        "functions": {
            "public.reset_retention_pressure_generation()": "6bb6069d5810baccb4d19a059e96e858cb78733cbb8923dc0f94c0a128207369",
            "public.reset_retention_pressure_privacy()": "09ac753bfddab272d9151e5ddadaad7cb3646164a3839bc84288c0720983ca9b",
        },
        "triggers": {
            "public.chat_contexts.chat_contexts_retention_pressure_generation": "a77da4c837f798244b7ff039a44270c1e82faa9f52723bd801beccde360b7e83",
            "public.privacy_subject_barriers.privacy_subject_barriers_retention_pressure": "cb238f3c89d8f56226c0f0bdec3be8c917676d39d1750636bb7e05c86b3e8b47",
        },
    }
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    after = json.loads(contract.CONTRACT.read_bytes())
    restored_catalog = deepcopy(after["catalog"])
    for section, additions in allowed.items():
        for key, expected in additions.items():
            assert digest(restored_catalog[section].pop(key)) == expected
    prior_kinds = "'normal'::text, 'greeting'::text, 'fortune_context_root'::text, 'fortune_derived'::text, 'topic_opening'::text"
    current_kinds = prior_kinds + ", 'safety_check'::text, 'safety'::text"
    constraint = restored_catalog["constraints"]["public.messages.messages_kind_check"]
    assert constraint == dict(deferrable=False, deferred=False, validated=True,
        enforcement_triggers=[], definition=f"CHECK ((kind = ANY (ARRAY[{current_kinds}])))")
    constraint["definition"] = f"CHECK ((kind = ANY (ARRAY[{prior_kinds}])))"
    assert digest(restored_catalog) == expected_main_catalog
    # Retain the original subscription-only df2 baseline attestation as well.
    for key in _KEYS:
        assert restored_catalog["columns"].pop(key) == _COL
    assert digest(restored_catalog) == _BASELINE_CATALOG_SHA256

    main_source = contract.SCHEMA.read_text()
    assert hashlib.sha256(main_source.encode()).hexdigest() == expected_guard_source
    assert after["schema_sha256"] == hashlib.sha256(main_source.encode()).hexdigest()
    for marker, terminator in (
        ("-- FUNCTION: public.reset_retention_pressure_generation()\n", "$function$;\n\n"),
        ("-- FUNCTION: public.reset_retention_pressure_privacy()\n", "$function$;\n\n"),
        ("-- TRIGGER: public.chat_contexts chat_contexts_retention_pressure_generation\n", "\n\n"),
        ("-- TRIGGER: public.privacy_subject_barriers privacy_subject_barriers_retention_pressure\n", "\n\n"),
        ("-- ACL: public.FUNCTION reset_retention_pressure_generation()\n", "\n\n"),
        ("-- ACL: public.FUNCTION reset_retention_pressure_privacy()\n", "\n\n"),
    ):
        assert main_source.count(marker) == 1
        begin = main_source.index(marker)
        end = main_source.index(terminator, begin) + len(terminator)
        main_source = main_source[:begin] + main_source[end:]
    for line in (
        "    contract_processed_through_message_id bigint DEFAULT 0 NOT NULL,\n",
        "    contract_generation bigint DEFAULT 0 NOT NULL,\n",
        "    retention_pressure_through_message_id bigint,\n",
        "    CONSTRAINT chat_contexts_retention_pressure_positive_check CHECK (((retention_pressure_through_message_id IS NULL) OR (retention_pressure_through_message_id > 0))),\n",
        "    state_json jsonb,\n", "    validation_json jsonb,\n", "    canonical_text text,\n",
        "    CONSTRAINT mem0_memory_registry_context_priority_check CHECK ((context_priority = ANY (ARRAY['related'::text, 'required'::text]))),\n",
        "    CONSTRAINT mem0_ingest_candidates_context_priority_check CHECK ((context_priority = ANY (ARRAY['related'::text, 'required'::text]))),\n",
    ):
        assert main_source.count(line) == 1
        main_source = main_source.replace(line, "", 1)
    priority = "    context_priority text DEFAULT 'related'::text NOT NULL,\n"
    assert main_source.count(priority) == 2
    main_source = main_source.replace(priority, "")
    assert main_source.count(current_kinds) == 1
    main_source = main_source.replace(current_kinds, prior_kinds, 1)
    assert hashlib.sha256(main_source.encode()).hexdigest() == expected_main_source
    main64_source = main_source
    for added, original in (
        ("    payload jsonb NOT NULL,\n    processing_state jsonb,\n", "    payload jsonb NOT NULL,\n"),
        ("    last_event_at timestamp with time zone,\n    access_evidence jsonb,\n",
         "    last_event_at timestamp with time zone,\n"),
    ):
        assert main_source.count(added) == 1
        main_source = main_source.replace(added, original, 1)
    assert hashlib.sha256(main_source.encode()).hexdigest() == _BASELINE_SOURCE_SHA256
    assert contract.subscription_schema_upgrade(main_source) == main64_source
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
