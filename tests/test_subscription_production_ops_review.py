"""Independent production-operations safety checks; no AWS, provider, or DB calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

from app.services import subscription_ops as ops
from scripts import run_subscription_ops as driver


@pytest.mark.parametrize("changed", ["before", "owner_id", "financial_sha256"])
async def test_fresh_preview_cannot_replace_approved_manifest_baseline(monkeypatch, tmp_path, changed):
    uid, sid = uuid.uuid4(), uuid.uuid4()
    sub = SimpleNamespace(**{key: None for key in ops.processing._FIELDS})
    sub.id, sub.user_id = sid, uid
    before = ops.repair.serial(ops.processing.baseline(sub))
    target = {"mode": "metadata", "subscription_id": str(sid), "owner_id": str(uid),
              "payment_id": str(uuid.uuid4()), "payment_tx": "synthetic-paid-tx", "payment_status": "paid",
              "before": before, "financial_sha256": "f" * 64}
    manifest = {"version": 1, "environment": "production", "project_ref": ops.PRODUCTION_PROJECT,
                "app_sha": "a" * 40, "targets": [target], "controls": [], "configuration_before": {}}
    folder = tmp_path / "synthetic-session"
    folder.mkdir()
    ops.write_private(folder / "manifest.json", manifest)
    plan = {"version": 1, "environment": "production", "project_ref": ops.PRODUCTION_PROJECT,
            "app_sha": "a" * 40, "mode": "metadata", "subscription_id": str(sid), "owner_id": str(uid),
            "payment_id": target["payment_id"], "payment_tx": target["payment_tx"],
            "before": deepcopy(before), "financial_sha256": "f" * 64, "tx_ids": ["synthetic-paid-tx"],
            "after": {"latest_transaction_id": "synthetic-paid-tx",
                      "access_evidence": {"current_transaction_id": "synthetic-paid-tx"}}}
    if changed == "before":
        plan["before"]["latest_transaction_id"] = "new-purchase-arrived-after-manifest-check"
    elif changed == "owner_id":
        plan["owner_id"] = str(uuid.uuid4())
    else:
        plan["financial_sha256"] = "e" * 64

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, identity, **kwargs):
            return sub

        async def rollback(self):
            pass

    monkeypatch.setattr(ops, "Path", lambda value: tmp_path if value == "/audit" else Path(value))
    monkeypatch.setattr(ops, "require_runtime", Mock())
    monkeypatch.setattr(ops, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(ops, "get_engine", lambda: SimpleNamespace(sync_engine=SimpleNamespace()))
    monkeypatch.setattr(ops, "verify_controls", AsyncMock())
    monkeypatch.setattr(ops.repair, "_financial_digest", AsyncMock(return_value="f" * 64))
    preview = AsyncMock(return_value=plan)
    monkeypatch.setattr(ops.repair, "preview", preview)
    apply = AsyncMock(return_value={"postcheck": "verified"})
    monkeypatch.setattr(ops.repair, "apply_preview", apply)
    args = SimpleNamespace(action="repair_metadata", expected_sha="a" * 40,
                           session="synthetic-session", expected_manifest=ops.repair.digest(manifest),
                           expected_membership=ops.membership(manifest), expected_counts=[0, 1, 0])
    with pytest.raises(ops.OperationStopped):
        await ops.operate(args)
    preview.assert_awaited_once()
    apply.assert_not_awaited()


def test_main_does_not_log_unexpected_exception_payload(monkeypatch, capsys):
    secret_error = "synthetic-provider-body customer=synthetic-user postgres://private-dsn"
    monkeypatch.setattr(ops.logging, "disable", Mock())
    monkeypatch.setattr(ops.os, "umask", Mock())
    operation = AsyncMock(side_effect=RuntimeError(secret_error))
    monkeypatch.setattr(ops, "operate", operation)
    monkeypatch.setattr(ops.sys, "argv", ["subscription_ops", "report", "--expected-sha", "a" * 40,
                                         "--session", "synthetic-session", "--expected-membership", "b" * 64,
                                         "--expected-counts", "1,7,29"])
    assert ops.main() == 1
    operation.assert_awaited_once()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert secret_error not in captured.out
    assert json.loads(captured.out) == {"ok": False, "reason": "operation_failed_review_private_journal"}


@pytest.mark.parametrize("invalid", ["development", "wrong_project", "missing_provider"])
def test_runtime_rejects_wrong_target_and_missing_read_credentials(monkeypatch, invalid):
    values = {"environment": "production", "git_sha": "a" * 40,
              "supabase_db_connection_string": "postgresql://postgres." + ops.PRODUCTION_PROJECT +
                  ":synthetic@aws-0-ap-northeast-2.pooler.supabase.com:6543/postgres",
              "supabase_url": "https://" + ops.PRODUCTION_PROJECT + ".supabase.co",
              "revenuecat_api_v2_key": "synthetic-read-key", "revenuecat_project_id": "synthetic-project"}
    if invalid == "development":
        values["environment"] = "development"
    elif invalid == "wrong_project":
        values["supabase_db_connection_string"] = values["supabase_db_connection_string"].replace(ops.PRODUCTION_PROJECT, "synthetic-other-project")
    else:
        values["revenuecat_api_v2_key"] = ""
    for key, value in values.items():
        monkeypatch.setattr(ops.settings, key, value)
    with pytest.raises(ops.OperationStopped):
        ops.require_runtime("a" * 40)


async def test_final_verification_rejects_new_pending_purchase_or_transfer(monkeypatch, tmp_path):
    uid, sid, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    sub = SimpleNamespace(**{key: None for key in ops.processing._FIELDS})
    sub.id, sub.user_id, sub.expires_at = sid, uid, expiry
    sub.rc_subscription_id = "synthetic-rc-object"
    pay = SimpleNamespace(id=pid, status="paid")
    baseline = ops.repair.serial(ops.processing.baseline(sub))
    receipt = {"status": "committed", "postcheck": "verified", "after": baseline}
    target = {"mode": "metadata", "subscription_id": str(sid), "owner_id": str(uid),
              "payment_id": str(pid), "payment_status": "paid", "payment_tx": "synthetic-paid-tx",
              "before": baseline, "financial_sha256": "f" * 64}

    async def get(model, identity, **kwargs):
        return sub if model is ops.Subscription else pay

    monkeypatch.setattr(ops, "last_committed", lambda path: receipt)
    monkeypatch.setattr(ops.repair, "_financial_digest", AsyncMock(return_value="f" * 64))
    pending = AsyncMock(return_value=True)
    monkeypatch.setattr(ops.repair, "_pending", pending)
    monkeypatch.setattr(ops.access, "resolve_access", lambda *args: SimpleNamespace(store_trial_ends_at=None))
    with pytest.raises(ops.OperationStopped):
        await ops.verify_targets(SimpleNamespace(get=get), {"targets": [target]}, tmp_path)
    pending.assert_awaited()


async def test_emergency_deactivate_does_not_depend_on_previous_release_manifest(monkeypatch, tmp_path):
    old_manifest = {"version": 1, "environment": "production", "project_ref": ops.PRODUCTION_PROJECT,
                    "app_sha": "a" * 40, "targets": [], "controls": [], "configuration_before": {}}
    folder = tmp_path / "synthetic-session"
    folder.mkdir()
    ops.write_private(folder / "manifest.json", old_manifest)
    enabled = SimpleNamespace(key=ops.processing.ENABLED, value=True)

    class Session:
        commit = AsyncMock()
        rollback = AsyncMock()
        execute = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, identity, **kwargs):
            assert model is ops.AppConfig and identity == ops.processing.ENABLED
            return enabled

    session = Session()
    monkeypatch.setattr(ops, "Path", lambda value: tmp_path if value == "/audit" else Path(value))
    monkeypatch.setattr(ops, "require_runtime", Mock())
    monkeypatch.setattr(ops, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(ops, "get_engine", lambda: SimpleNamespace(sync_engine=SimpleNamespace()))
    monkeypatch.setattr(ops.subscription, "_ownership_lock", AsyncMock())
    monkeypatch.setattr(ops, "config_values", AsyncMock(return_value={ops.processing.ENABLED: True}))
    args = SimpleNamespace(action="deactivate", expected_sha="b" * 40, session="synthetic-session",
                           expected_manifest=ops.repair.digest(old_manifest),
                           expected_membership=ops.membership(old_manifest), expected_counts=[0, 0, 0])
    result = await ops.operate(args)
    assert result["ok"] is True and enabled.value is False
    session.commit.assert_awaited_once()


async def test_configuration_audit_must_be_durable_before_database_commit(monkeypatch, tmp_path):
    manifest = {"version": 1, "environment": "production", "project_ref": ops.PRODUCTION_PROJECT,
                "app_sha": "a" * 40, "targets": [], "controls": [], "configuration_before": {}}
    folder = tmp_path / "synthetic-session"
    folder.mkdir()
    ops.write_private(folder / "manifest.json", manifest)
    enabled = SimpleNamespace(key=ops.processing.ENABLED, value=True)

    class Session:
        commit = AsyncMock()
        rollback = AsyncMock()
        execute = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, identity, **kwargs):
            return enabled

    session = Session()
    monkeypatch.setattr(ops, "Path", lambda value: tmp_path if value == "/audit" else Path(value))
    monkeypatch.setattr(ops, "require_runtime", Mock())
    monkeypatch.setattr(ops, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(ops, "get_engine", lambda: SimpleNamespace(sync_engine=SimpleNamespace()))
    monkeypatch.setattr(ops.subscription, "_ownership_lock", AsyncMock())
    monkeypatch.setattr(ops, "config_values", AsyncMock(return_value={ops.processing.ENABLED: True}))
    monkeypatch.setattr(ops.os, "fsync", Mock(side_effect=OSError("synthetic audit storage unavailable")))
    args = SimpleNamespace(action="deactivate", expected_sha="a" * 40, session="synthetic-session",
                           expected_manifest=ops.repair.digest(manifest),
                           expected_membership=ops.membership(manifest), expected_counts=[0, 0, 0])
    with pytest.raises((OSError, ops.OperationStopped)):
        await ops.operate(args)
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("timers", [(False, False), (True, True)])
def test_driver_never_executes_when_worker_timer_count_is_not_one(monkeypatch, timers):
    identities = ["i-synthetic-a", "i-synthetic-b"]

    def aws(service, *args):
        if service == "sts":
            return {"Account": driver.ACCOUNT, "Arn": f"arn:aws:sts::{driver.ACCOUNT}:assumed-role/{driver.ROLE}/synthetic"}
        if service == "ec2":
            return {"Reservations": [{"Instances": [{"InstanceId": identity} for identity in identities]}]}
        assert service == "elbv2"
        return {"TargetHealthDescriptions": [{"Target": {"Id": identity}, "TargetHealth": {"State": "healthy"}}
                                              for identity in identities]}

    modes = []

    def ssm(identity, config):
        modes.append(config["mode"])
        return {"ok": True, "timer": timers[identities.index(identity)]}

    monkeypatch.setattr(driver, "require_runner", Mock())
    monkeypatch.setattr(driver, "aws", aws)
    monkeypatch.setattr(driver, "ssm", ssm)
    args = SimpleNamespace(action="report", expected_sha="a" * 40, session="synthetic-session",
                           expected_manifest="", expected_membership="b" * 64, expected_counts="1,7,29")
    with pytest.raises(driver.Stopped):
        driver.run(args)
    assert "execute" not in modes
