"""Independent driver tests. All subprocess, AWS, SSM and remote filesystem work is fake."""
import builtins
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import run_subscription_ops as driver


def _args(action="report"):
    return SimpleNamespace(action=action, expected_sha="a" * 40, session="synthetic-session",
                           expected_manifest="" if action == "report" else "b" * 64,
                           expected_membership="c" * 64, expected_counts="1,7,29")


@pytest.fixture
def runner(monkeypatch):
    for name in ("GITHUB_ACTIONS", "GITHUB_REF", "GITHUB_REPOSITORY", "AWS_PROFILE",
                 "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    values = {"GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/main",
              "GITHUB_REPOSITORY": "SOMA-OneTwoThree/moly-backend",
              "AWS_ACCESS_KEY_ID": "synthetic", "AWS_SESSION_TOKEN": "synthetic"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.mark.parametrize("name,value", [
    ("GITHUB_ACTIONS", "false"), ("GITHUB_REF", "refs/heads/dev"),
    ("GITHUB_REPOSITORY", "unconfirmed/repo"), ("AWS_PROFILE", "personal"),
    ("AWS_ACCESS_KEY_ID", ""), ("AWS_SESSION_TOKEN", ""),
])
def test_unconfirmed_runner_stops_before_even_identity_or_subprocess(runner, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    aws, subprocess = Mock(), Mock()
    monkeypatch.setattr(driver, "aws", aws)
    monkeypatch.setattr(driver.subprocess, "run", subprocess)
    with pytest.raises(driver.Stopped, match="oidc_runner"):
        driver.run(_args())
    aws.assert_not_called()
    subprocess.assert_not_called()


@pytest.fixture
def fleet(runner, monkeypatch):
    state = {"account": driver.ACCOUNT, "arn": f"arn:aws:sts::{driver.ACCOUNT}:assumed-role/{driver.ROLE}/synthetic",
             "instances": ["i-synthetic-a", "i-synthetic-b"], "health": ["healthy", "healthy"]}

    def aws(*argv):
        if argv[:2] == ("sts", "get-caller-identity"):
            return {"Account": state["account"], "Arn": state["arn"]}
        if argv[:2] == ("ec2", "describe-instances"):
            return {"Reservations": [{"Instances": [{"InstanceId": identity} for identity in state["instances"]]}]}
        if argv[:2] == ("elbv2", "describe-target-health"):
            return {"TargetHealthDescriptions": [
                {"Target": {"Id": identity}, "TargetHealth": {"State": health}}
                for identity, health in zip(state["instances"], state["health"])
            ]}
        raise AssertionError("unexpected mocked AWS API")

    spy = Mock(side_effect=aws)
    monkeypatch.setattr(driver, "aws", spy)
    return state, spy


@pytest.mark.parametrize("key,value", [("account", "unconfirmed"), ("arn", "arn:aws:iam::676972757138:user/other")])
def test_only_fixed_project_account_and_assumed_role_are_accepted(fleet, monkeypatch, key, value):
    state, aws = fleet
    state[key] = value
    ssm = Mock()
    monkeypatch.setattr(driver, "ssm", ssm)
    with pytest.raises(driver.Stopped, match="identity_mismatch"):
        driver.run(_args())
    assert aws.call_count == 1
    ssm.assert_not_called()


@pytest.mark.parametrize("key,value", [
    ("expected_sha", "a" * 40 + ";synthetic"), ("session", "$(synthetic-command)"),
    ("expected_membership", "synthetic\ncommand"), ("expected_counts", "1,7,29;synthetic"),
    ("expected_manifest", "not-a-reviewed-hash"),
])
def test_unreviewed_operation_inputs_stop_before_any_project_call(runner, monkeypatch, key, value):
    args = _args("activate")
    setattr(args, key, value)
    aws = Mock()
    monkeypatch.setattr(driver, "aws", aws)
    with pytest.raises(driver.Stopped, match="invalid_operation_arguments"):
        driver.run(args)
    aws.assert_not_called()


@pytest.mark.parametrize("timers", [(False, False), (True, True)])
def test_exactly_one_worker_timer_required_before_execute(fleet, monkeypatch, timers):
    calls = []

    def ssm(host, config):
        calls.append((host, dict(config)))
        return {"ok": True, "timer": timers[0 if host.endswith("a") else 1]}

    monkeypatch.setattr(driver, "ssm", ssm)
    with pytest.raises(driver.Stopped, match="worker_ownership"):
        driver.run(_args())
    assert len(calls) == 2 and all(config["mode"] == "check" for _, config in calls)


def test_unhealthy_target_prevents_all_ssm_execution(fleet, monkeypatch):
    fleet[0]["health"][0] = "draining"
    ssm = Mock()
    monkeypatch.setattr(driver, "ssm", ssm)
    with pytest.raises(driver.Stopped, match="fleet_not_healthy"):
        driver.run(_args())
    ssm.assert_not_called()


def test_all_hosts_are_checked_before_stable_host_executes(fleet, monkeypatch, capsys):
    calls = []

    def ssm(host, config):
        calls.append((host, dict(config)))
        return {"ok": True, "timer": host.endswith("a")} if config["mode"] == "check" else {"ok": True}

    monkeypatch.setattr(driver, "ssm", ssm)
    assert driver.run(_args()) == 0
    assert [(host, config["mode"]) for host, config in calls] == [
        ("i-synthetic-a", "check"), ("i-synthetic-b", "check"), ("i-synthetic-a", "execute")]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_emergency_deactivate_can_fall_back_after_first_host_transport_failure(fleet, monkeypatch, capsys):
    # Deactivation must not depend on old manifests, provider credentials or TG health.
    fleet[0]["health"] = ["unhealthy", "unhealthy"]
    calls = []

    def ssm(host, config):
        calls.append((host, dict(config)))
        if host.endswith("a"):
            raise driver.Stopped("remote_operation_failed_review_private_journal")
        return {"ok": True, "new_adoption_enabled": False}

    monkeypatch.setattr(driver, "ssm", ssm)
    args = _args("deactivate")
    args.expected_manifest = args.expected_membership = args.expected_counts = ""
    assert driver.run(args) == 0
    assert len(calls) == 2 and all(config["mode"] == "execute" for _, config in calls)
    assert not any(call.args[:2] == ("elbv2", "describe-target-health") for call in fleet[1].call_args_list)
    assert json.loads(capsys.readouterr().out)["new_adoption_enabled"] is False


@pytest.mark.parametrize("payload", [
    {"ok": True, "user_id": "synthetic-private-marker"},
    {"ok": True, "reason": "synthetic-private-marker@example.test"},
    {"ok": True, "sha": "not-a-reviewed-sha"},
    {"ok": True, "counts": {"refunded": True, "metadata": 7, "controls": 29}},
])
def test_local_summary_rejects_unapproved_data(payload):
    with pytest.raises(driver.Stopped):
        driver.safe_summary(payload)


def test_remote_filters_admin_summary_before_it_reaches_ssm_stdout(capsys):
    # Execute only the fixed REMOTE program with fake imported modules. No real
    # subprocess, Docker, systemctl, chown, filesystem, AWS or DB access is possible.
    private_marker = "synthetic-private-marker"

    class AuditPath:
        parents = ()

        def __init__(self, *_):
            pass

        def __truediv__(self, _):
            return self

        def is_symlink(self):
            return False

        def exists(self):
            return False

        def mkdir(self, **_):
            pass

        def __str__(self):
            return "/synthetic-audit"

    def command(argv, **kwargs):
        assert kwargs.get("capture_output") is True
        stdout = "inactive" if argv[0] == "systemctl" else "1000"
        if "app.services.subscription_ops" in argv:
            stdout = json.dumps({"ok": True, "user_id": private_marker})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=private_marker)

    modules = {"json": json, "os": SimpleNamespace(chown=Mock(), chmod=Mock()),
               "pathlib": SimpleNamespace(Path=AuditPath), "subprocess": SimpleNamespace(run=command)}

    def safe_import(name, *args, **kwargs):
        return modules[name] if name in modules else builtins.__import__(name, *args, **kwargs)

    config = {"sha": "a" * 40, "action": "deactivate", "mode": "execute", "session": "synthetic-session",
              "manifest": "", "membership": "", "counts": ""}
    source = driver.REMOTE.replace("CONFIG_LITERAL", repr(json.dumps(config)))
    exec(compile(source, "<mocked-fixed-remote>", "exec"),
         {"__builtins__": {**vars(builtins), "__import__": safe_import}})
    output = capsys.readouterr().out
    assert private_marker not in output
    assert json.loads(output)["ok"] is False


def test_workflow_serializes_operations_with_production_deploy_and_uses_fixed_oidc_role():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/subscription-ops.yml").read_text()
    deployment = (root / ".github/workflows/deploy.yml").read_text()
    assert "group: deploy-moly-backend\n  cancel-in-progress: false" in workflow
    assert "group: deploy-moly-backend\n  cancel-in-progress: false" in deployment
    assert f"role-to-assume: arn:aws:iam::{driver.ACCOUNT}:role/{driver.ROLE}" in workflow
    assert "id-token: write" in workflow and "if: github.ref == 'refs/heads/main'" in workflow
    run_body = workflow.split("        run: |", 1)[1]
    assert "${{" not in run_body  # Dispatch values enter only quoted environment arguments.
