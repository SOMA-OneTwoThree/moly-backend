"""Independent repair safety regressions. All DB/provider operations are fakes."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from app.services import revenuecat, subscription, subscription_repair as repair
from app.services import subscription_processing as processing


class Session:
    def __init__(self, sub=None):
        self.sub, self.commits = sub, 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, *args, **kwargs):
        pass

    async def get(self, *args, **kwargs):
        return self.sub

    async def rollback(self):
        pass

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


@pytest.fixture
def prepared(monkeypatch):
    current = datetime(2030, 1, 1, tzinfo=timezone.utc)

    class Clock:
        value = current

        @classmethod
        def now(cls, *args):
            return cls.value

    monkeypatch.setattr(repair, "datetime", Clock)
    # serial() uses isinstance, so preserve the real datetime type there.
    original_serial = repair.serial

    def serial(value):
        if isinstance(value, datetime):
            return value.isoformat()
        return original_serial(value)

    monkeypatch.setattr(repair, "serial", serial)
    sub = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), rc_subscription_id="synthetic-rc",
                          plan="yearly", status="active", original_transaction_id="synthetic-original",
                          latest_transaction_id="synthetic-paid", purchased_at=current - timedelta(days=1),
                          expires_at=current + timedelta(days=365), store_trial_ends_at=None,
                          last_event_at=current - timedelta(days=1), auto_renew_enabled=False,
                          environment="SANDBOX", ownership_changed_at=None, access_evidence=None)
    proof = {"snapshot": {"id": "synthetic-rc"}, "transactions": [], "objects": []}
    plan = {"version": 1, "environment": "development", "project_ref": "synthetic-dev",
            "app_sha": "a" * 40, "created_at": current.isoformat(), "mode": "metadata",
            "subscription_id": str(sub.id), "owner_id": str(sub.user_id), "tx_ids": [],
            "before": repair.serial(processing.baseline(sub)),
            "after": repair.serial({name: getattr(sub, name) for name in repair._WRITABLE}),
            "provider_sha256": repair.digest(proof), "financial_sha256": "synthetic-finance"}

    async def nothing(*args, **kwargs):
        pass

    async def pending(*args, **kwargs):
        return False

    async def financial(*args, **kwargs):
        return "synthetic-finance"

    monkeypatch.setattr(subscription, "_ownership_lock", nothing)
    monkeypatch.setattr(repair, "advisory_xact_lock", nothing)
    monkeypatch.setattr(repair, "_pending", pending)
    monkeypatch.setattr(repair, "_financial_digest", financial)
    return SimpleNamespace(clock=Clock, now=current, sub=sub, plan=plan, proof=proof,
                           session=Session(sub))


async def test_preview_that_expires_during_provider_read_cannot_commit(prepared, monkeypatch):
    p = prepared
    p.plan["created_at"] = (p.now - timedelta(seconds=50)).isoformat()

    async def provider(*args):
        p.clock.value = p.now + timedelta(seconds=20)
        return p.proof

    monkeypatch.setattr(repair, "_provider_proof", provider)
    with pytest.raises(repair.access.EvidenceError, match="expired|stale|60|fresh"):
        await repair.apply_preview(p.session, p.plan, expected_sha256=repair.digest(p.plan),
                                   project_ref="synthetic-dev", app_sha="a" * 40)
    assert p.session.commits == 0


async def test_postcommit_provider_outage_reports_committed_outcome(prepared, monkeypatch):
    p, calls = prepared, 0

    async def provider(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise revenuecat.RevenueCatError("synthetic postcheck outage")
        return p.proof

    monkeypatch.setattr(repair, "_provider_proof", provider)
    try:
        result = await repair.apply_preview(p.session, p.plan, expected_sha256=repair.digest(p.plan),
                                           project_ref="synthetic-dev", app_sha="a" * 40)
    except Exception as error:
        assert "committed" in str(error).lower(), "postcommit errors must disclose that the repair committed"
    else:
        assert result.get("applied_at") and result.get("postcheck_provider_unchanged") is not True
    assert p.session.commits == 1


async def test_postcommit_audit_outage_explicitly_reports_committed(prepared, monkeypatch):
    p, statuses = prepared, []

    async def provider(*args):
        return p.proof

    def record(value):
        statuses.append(value["status"])
        if value["status"] == "committed":
            raise OSError("synthetic disk outage")

    monkeypatch.setattr(repair, "_provider_proof", provider)
    with pytest.raises(repair.RepairCommitted, match="committed"):
        await repair.apply_preview(p.session, p.plan, expected_sha256=repair.digest(p.plan),
                                   project_ref="synthetic-dev", app_sha="a" * 40, on_status=record)
    assert statuses == ["commit_pending", "committed"]
    assert p.session.commits == 1


async def test_lost_commit_response_preserves_uncertain_journal_state(prepared, monkeypatch):
    p, statuses = prepared, []

    async def provider(*args):
        return p.proof

    async def uncertain_commit():
        p.session.commits += 1
        raise OSError("synthetic connection loss during commit")

    monkeypatch.setattr(repair, "_provider_proof", provider)
    monkeypatch.setattr(p.session, "commit", uncertain_commit)
    with pytest.raises(repair.RepairCommitUncertain, match="unknown"):
        await repair.apply_preview(p.session, p.plan, expected_sha256=repair.digest(p.plan),
                                   project_ref="synthetic-dev", app_sha="a" * 40,
                                   on_status=lambda value: statuses.append(value["status"]))
    assert statuses == ["commit_pending", "commit_result_unknown"]
    assert p.session.commits == 1


async def test_new_local_inbox_after_commit_cannot_report_verified(prepared, monkeypatch):
    p = prepared

    async def provider(*args):
        return p.proof  # The provider can lag behind a newly received local webhook.

    async def pending(*args, **kwargs):
        return p.session.commits > 0

    monkeypatch.setattr(repair, "_provider_proof", provider)
    monkeypatch.setattr(repair, "_pending", pending)
    result = await repair.apply_preview(p.session, p.plan, expected_sha256=repair.digest(p.plan),
                                       project_ref="synthetic-dev", app_sha="a" * 40)
    assert p.session.commits == 1
    assert result["postcheck"] != "verified"


async def test_new_local_purchase_after_commit_is_not_restored_or_reported_verified(prepared, monkeypatch):
    p = prepared

    async def provider(*args):
        return p.proof

    async def commit_and_receive_purchase():
        p.session.commits += 1
        p.sub.latest_transaction_id = "synthetic-new-valid-purchase"

    monkeypatch.setattr(repair, "_provider_proof", provider)
    monkeypatch.setattr(p.session, "commit", commit_and_receive_purchase)
    result = await repair.apply_preview(p.session, p.plan, expected_sha256=repair.digest(p.plan),
                                       project_ref="synthetic-dev", app_sha="a" * 40)
    assert p.session.commits == 1
    assert result["postcheck"] != "verified"
    assert p.sub.latest_transaction_id == "synthetic-new-valid-purchase"


@pytest.fixture
def cli(monkeypatch, tmp_path):
    from app.core import db
    from db import envfile
    import httpx

    path = Path(__file__).resolve().parents[1] / "scripts/subscription_access_repair.py"
    # The CLI environment bootstrap is irrelevant to these isolated orchestration tests.
    monkeypatch.setattr(envfile, "bootstrap_script_environment", lambda *_: None)
    spec = importlib.util.spec_from_file_location("isolated_subscription_repair_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = {"applies": 0, "health_env": "development"}
    monkeypatch.setattr(envfile, "configure_application_db", lambda *_: "synthetic-dsn")
    monkeypatch.setattr(envfile, "assert_dev_target", lambda *_: None)
    monkeypatch.setattr(envfile, "project_ref", lambda *_: "synthetic-dev")
    monkeypatch.setattr(db, "get_sessionmaker", lambda: lambda: Session())

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            assert url == "https://dev.moly.asia/health"
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"version": "a" * 40, "env": state["health_env"]})

    async def apply(*args, **kwargs):
        state["applies"] += 1
        return {"postcheck_provider_unchanged": True, "applied_at": "synthetic"}

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(repair, "apply_preview", apply)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"synthetic": True}))
    args = SimpleNamespace(env="dev", expected_app_sha="a" * 40, action="apply", plan=plan,
                           expected_sha256="b" * 64, output=tmp_path / "audit.json")
    return module, state, args


async def test_existing_audit_output_is_rejected_before_apply(cli):
    module, state, args = cli
    args.output.write_text("existing private audit")
    with pytest.raises((ValueError, FileExistsError)):
        await module.run(args)
    assert state["applies"] == 0
    assert args.output.read_text() == "existing private audit"


async def test_health_environment_must_be_development_even_with_matching_sha(cli):
    module, state, args = cli
    state["health_env"] = "production"
    with pytest.raises(ValueError, match="development|environment"):
        await module.run(args)
    assert state["applies"] == 0


def test_partial_audit_write_preserves_prior_durable_record(cli):
    module, _, args = cli
    previous = {"status": "commit_pending", "synthetic": True}
    original_dump = module.json.dump

    def interrupted(value, stream, **kwargs):
        stream.write('{"status":')
        raise OSError("synthetic interrupted journal write")

    with module.reserve_private(args.output) as stream:
        module.private_write(stream, previous)
        module.json.dump = interrupted
        try:
            with pytest.raises(OSError):
                module.private_write(stream, {"status": "committed"})
        finally:
            module.json.dump = original_dump
    lines = args.output.read_text().splitlines()
    assert json.loads(lines[0]) == previous
