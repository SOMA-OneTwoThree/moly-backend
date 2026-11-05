"""Production controller tests use synthetic receipts and fake sessions only."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from scripts import activate_chat_content_crypto as c

SHA, RESCUE = "a" * 40, "b" * 40
IDS = ["i-11111111", "i-22222222"]
MODE = {"started": False, "active_key_id": 1, "format_version": 1, "required_key_ids": [1]}
CURRENT = {
    "capability": "chat-content-aead-v1",
    "environment": "production",
    "version": SHA,
    "started": False,
    "format_version": 1,
    "active_key_id": 1,
    "key_fingerprints": {"1": "c" * 64},
    "schema": "ok",
    "self_test": "ok",
}


def snapshot():
    return {
        key: {"exists": key == c.STATE_KEY, "value": deepcopy(MODE) if key == c.STATE_KEY else None}
        for key in c.CONFIG_KEYS
    }


def receipt(environment="production"):
    ids = IDS if environment == "production" else IDS[:1]
    current = {**CURRENT, "environment": environment}
    identity = dict(
        environment=environment,
        project_ref=c.PROJECTS[environment],
        release_sha=SHA,
        release_id="1234",
    )
    common = {**identity, "origin": "trusted-deploy-workflow", "status": "passed"}
    instances = [
        {"instance_id": id, "api": deepcopy(current), "consumer": deepcopy(current)} for id in ids
    ]
    rescue_proof = {**current, "version": RESCUE}
    gates = {name: deepcopy(common) for name in c.GATE_NAMES}
    gates["rollback_reader"].update(
        rescue_sha=RESCUE,
        instances=[
            {"instance_id": id, "api": deepcopy(rescue_proof), "consumer": deepcopy(rescue_proof)}
            for id in ids
        ],
        worker={"instance_id": ids[0], "proof": deepcopy(rescue_proof)},
    )
    return {
        **identity,
        "format": c.FORMAT,
        "instances": instances,
        "worker": {
            "instance_id": ids[0],
            "proof": deepcopy(current),
            "old_tick_drained": True,
            "timer_restored": True,
        },
        "gates": gates,
        "config_snapshot": snapshot(),
    }


def validate(value, environment="production"):
    c.validate_release(
        value,
        environment=environment,
        project_ref=c.PROJECTS[environment],
        release_sha=SHA,
        release_id="1234",
        instance_ids=IDS if environment == "production" else IDS[:1],
        worker_instance_id=IDS[0],
        current={**CURRENT, "environment": environment},
        expected_rescue_sha=RESCUE,
    )


@pytest.mark.parametrize("environment", ["production", "development"])
def test_exact_fleet_and_rescue_pass(environment):
    validate(receipt(environment), environment)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_instance",
        "duplicate_instance",
        "missing_consumer",
        "missing_worker",
        "not_drained",
        "not_restored",
        "old_sha",
        "wrong_environment",
        "wrong_key",
        "bool_key",
        "bool_format",
        "missing_gate",
        "failed_gate",
        "foreign_release",
        "rescue_no_proof",
        "rescue_old_sha",
        "rescue_wrong_key",
        "rescue_missing_role",
        "rescue_single_instance",
        "rescue_worker_missing",
        "guard_bad_type",
    ],
)
def test_no_weak_fleet_receipt_can_adopt(mutation):
    value = receipt()
    if mutation == "missing_instance":
        value["instances"].pop()
    elif mutation == "duplicate_instance":
        value["instances"][1] = deepcopy(value["instances"][0])
    elif mutation == "missing_consumer":
        del value["instances"][0]["consumer"]
    elif mutation == "missing_worker":
        value["worker"] = {}
    elif mutation == "not_drained":
        value["worker"]["old_tick_drained"] = False
    elif mutation == "not_restored":
        value["worker"]["timer_restored"] = False
    elif mutation == "old_sha":
        value["instances"][1]["api"]["version"] = RESCUE
    elif mutation == "wrong_environment":
        value["instances"][0]["api"]["environment"] = "development"
    elif mutation == "wrong_key":
        value["instances"][1]["consumer"]["key_fingerprints"]["1"] = "d" * 64
    elif mutation == "bool_key":
        value["instances"][1]["consumer"]["active_key_id"] = True
    elif mutation == "bool_format":
        value["instances"][1]["consumer"]["format_version"] = True
    elif mutation == "missing_gate":
        del value["gates"]["health"]
    elif mutation == "failed_gate":
        value["gates"]["daily_report"]["status"] = "failed"
    elif mutation == "foreign_release":
        value["gates"]["schema"]["release_id"] = "9876"
    elif mutation == "rescue_no_proof":
        value["gates"]["rollback_reader"] = {"status": "passed"}
    elif mutation == "rescue_old_sha":
        value["gates"]["rollback_reader"]["instances"][0]["api"]["version"] = SHA
    elif mutation == "rescue_wrong_key":
        value["gates"]["rollback_reader"]["worker"]["proof"]["key_fingerprints"]["1"] = "d" * 64
    elif mutation == "rescue_missing_role":
        del value["gates"]["rollback_reader"]["instances"][0]["consumer"]
    elif mutation == "rescue_single_instance":
        value["gates"]["rollback_reader"]["instances"].pop()
    elif mutation == "rescue_worker_missing":
        value["gates"]["rollback_reader"]["worker"] = {}
    elif mutation == "guard_bad_type":
        value["config_snapshot"]["crisis_guard_enabled"] = {"exists": True, "value": 1}
    with pytest.raises((c.ReleaseBlocked, ValueError)):
        validate(value)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres.qkgjlgzsharnilxnkytd:x@localhost/postgres",
        "postgresql://postgres.wywzjslvxwttxkecbyis:x@aws-0-test.pooler.supabase.com/postgres",
        "postgresql://postgres.qkgjlgzsharnilxnkytd:x@aws-0-test.pooler.supabase.com/postgres?host=elsewhere",
        "postgresql://postgres.qkgjlgzsharnilxnkytd:x@aws-0-test.pooler.supabase.com/other",
        "postgresql://postgres.qkgjlgzsharnilxnkytd:x@aws-0-test.pooler.supabase.com:5555/postgres",
    ],
)
def test_strict_approved_target(dsn):
    with pytest.raises(c.ReleaseBlocked):
        c.require_target("production", c.PROJECTS["production"], dsn)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class Session:
    def __init__(self, config=None):
        self.config = deepcopy(snapshot() if config is None else config)
        self.calls = []
        self.updates = []
        self.commit = AsyncMock()

    async def execute(self, sql, params=None):
        self.calls.append(str(sql))
        if str(sql).startswith("SELECT key,value"):
            return Result(
                [(k, deepcopy(v["value"])) for k, v in self.config.items() if v["exists"]]
            )
        if str(sql).startswith("INSERT"):
            self.updates.extend(json.loads(params["rows"]))
        return Result([])


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(c, "initialize_content_crypto", lambda **kw: SimpleNamespace())


async def test_atomic_config_batch_after_cas_and_missing_row_fence():
    session = Session()
    mode = await c.activate_release(session, receipt=receipt(), current=CURRENT, expected_sha=SHA)
    assert mode.started and len(session.updates) == 5
    assert session.calls[1] == "LOCK TABLE public.app_config IN SHARE ROW EXCLUSIVE MODE NOWAIT"
    assert "FOR UPDATE NOWAIT" in session.calls[2]
    assert sum(sql.startswith("INSERT") for sql in session.calls) == 1
    updates = {row["key"]: row["value"] for row in session.updates}
    assert updates[c.STATE_KEY]["started"] is True
    assert all(updates[k] == v for k, v in c.GUARD.items())
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("change", ["required", "active", "guard", "history", "missing_mode"])
async def test_concurrent_config_change_fails_before_any_write(change):
    session = Session()
    if change == "required":
        session.config[c.STATE_KEY]["value"]["required_key_ids"] = [1, 2]
    elif change == "active":
        session.config[c.STATE_KEY]["value"].update(active_key_id=2, required_key_ids=[1, 2])
    elif change == "missing_mode":
        session.config[c.STATE_KEY] = {"exists": False, "value": None}
    elif change == "history":
        session.config["crisis_guard_history_messages"] = {"exists": True, "value": 3}
    else:
        session.config["crisis_guard_policy_version"] = {"exists": True, "value": "other-policy"}
    with pytest.raises((c.ReleaseBlocked, ValueError)):
        await c.activate_release(session, receipt=receipt(), current=CURRENT, expected_sha=SHA)
    assert not session.updates


async def test_compatible_ack_lost_retry_is_noop_but_never_plaintext_downgrade():
    config = snapshot()
    config[c.STATE_KEY]["value"].update(started=True, activated_at="prior", release_sha=RESCUE)
    for key, value in c.GUARD.items():
        config[key] = {"exists": True, "value": value}
    session = Session(config)
    mode = await c.activate_release(
        session, receipt=receipt(), current={**CURRENT, "started": True}, expected_sha=SHA
    )
    assert mode.started and not session.updates


async def test_validation_failure_never_opens_write_session(monkeypatch):
    monkeypatch.setattr(c.settings, "environment", "production")
    monkeypatch.setattr(
        c.settings,
        "supabase_db_connection_string",
        "postgresql://postgres.qkgjlgzsharnilxnkytd:x@aws-0-test.pooler.supabase.com/postgres",
    )
    monkeypatch.setattr(c, "check", AsyncMock(return_value=CURRENT))
    maker = AsyncMock()
    monkeypatch.setattr(c, "get_sessionmaker", maker)
    value = receipt()
    value["gates"]["health"]["status"] = "failed"
    with pytest.raises(c.ReleaseBlocked):
        await c.run(
            environment="production",
            expected_project_ref=c.PROJECTS["production"],
            expected_sha=SHA,
            release_id="1234",
            instance_ids=IDS,
            worker_instance_id=IDS[0],
            receipt=value,
            expected_rescue_sha=RESCUE,
        )
    maker.assert_not_called()


async def test_success_commits_one_atomic_batch_after_all_proofs(monkeypatch):
    monkeypatch.setattr(c.settings, "environment", "production")
    monkeypatch.setattr(
        c.settings,
        "supabase_db_connection_string",
        "postgresql://postgres.qkgjlgzsharnilxnkytd:x@aws-0-test.pooler.supabase.com/postgres",
    )
    monkeypatch.setattr(c, "check", AsyncMock(return_value=CURRENT))
    session = Session()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(c, "get_sessionmaker", lambda: Context)
    result = await c.run(
        environment="production",
        expected_project_ref=c.PROJECTS["production"],
        expected_sha=SHA,
        release_id="1234",
        instance_ids=IDS,
        worker_instance_id=IDS[0],
        receipt=receipt(),
        expected_rescue_sha=RESCUE,
    )
    assert result["started"] is True and result["guard_enabled"] is True
    assert len(session.updates) == 5
    session.commit.assert_awaited_once()


async def test_commit_is_inside_absolute_one_second_bound(monkeypatch):
    import asyncio

    monkeypatch.setattr(c.settings, "environment", "production")
    monkeypatch.setattr(
        c.settings,
        "supabase_db_connection_string",
        "postgresql://postgres.qkgjlgzsharnilxnkytd:x@aws-0-test.pooler.supabase.com/postgres",
    )
    monkeypatch.setattr(c, "check", AsyncMock(return_value=CURRENT))
    session = Session()

    async def unavailable_commit():
        await asyncio.Event().wait()

    session.commit = AsyncMock(side_effect=unavailable_commit)

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(c, "get_sessionmaker", lambda: Context)
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await c.run(
            environment="production",
            expected_project_ref=c.PROJECTS["production"],
            expected_sha=SHA,
            release_id="1234",
            instance_ids=IDS,
            worker_instance_id=IDS[0],
            receipt=receipt(),
            expected_rescue_sha=RESCUE,
        )
    assert asyncio.get_running_loop().time() - started < 1.5
    session.commit.assert_awaited_once()
