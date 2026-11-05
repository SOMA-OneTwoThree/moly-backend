"""Actual fixed host program and fake cloud preserve only typed report failures."""

import json
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from scripts import chat_crypto_release_workflow as w
from tests.test_chat_crypto_release_workflow import setup_args, FakeCloud, IDS, SHA, INFRA, runtime


def failure():
    return {
        "daily_report": "failed",
        "environment": "production",
        "version": SHA,
        "read_only": True,
        "slack_sent": False,
        "elapsed_ms": 3100,
        "error": "ValueError",
        "diagnostic_format": "daily-report-diagnostics-v1",
        "diagnostics_truncated": False,
        "failures": [
            {
                "phase": "metric",
                "metric": "activity",
                "exception": "DBAPIError",
                "sqlstate": "57014",
                "elapsed_ms": 2501,
                "attempt": 2,
                "retry": False,
            }
        ],
    }


@pytest.mark.parametrize("host", IDS)
def test_either_candidate_host_failure_prevents_all_activation(tmp_path, monkeypatch, host):
    args = setup_args(tmp_path, monkeypatch)

    class Cloud(FakeCloud):
        def remote(self, identity, config):
            result = super().remote(identity, config)
            if config["action"] == "preflight" and identity == host and config["sha"] == SHA:
                result["daily_report"] = failure()
            return result

    cloud = Cloud("production")
    with pytest.raises(w.Blocked, match="candidate_daily_report_failed"):
        w.prepare(args, cloud, w.arguments(args))
    assert not Path(args.state).exists() and cloud.receipt is None
    assert all(row[0] not in {"collect", "activate"} for row in cloud.calls)


@pytest.mark.parametrize("invalid", [False, True])
def test_cloud_readback_never_surfaces_remote_stderr_or_extra_fields(monkeypatch, invalid):
    meta = failure()
    if invalid:
        meta["failures"][0]["raw_query"] = "private-canary"
    response = {
        "status": "blocked",
        "reason": "daily_report_failed",
        "action": "collect",
        "stage": "daily_report",
        "daily_report": meta,
    }
    cloud = object.__new__(w.Cloud)
    results = iter(
        [
            {"Command": {"CommandId": "11111111-1111-1111-1111-111111111111"}},
            {
                "Status": "Failed",
                "StandardOutputContent": json.dumps(response),
                "StandardErrorContent": "private-canary",
            },
        ]
    )
    monkeypatch.setattr(cloud, "aws", lambda *a: next(results))
    monkeypatch.setattr(w.time, "sleep", lambda _: None)
    with pytest.raises(w.Blocked) as caught:
        cloud.remote(
            IDS[0],
            {"action": "collect", "environment": "production", "sha": SHA, "release_id": "1234"},
        )
    if invalid:
        assert str(caught.value) == "remote_step_failed" and caught.value.diagnostics is None
    else:
        assert caught.value.diagnostics["daily_report"]["failures"][0]["metric"] == "activity"
        assert caught.value.diagnostics["operation"]["instance_id"] == IDS[0]
        assert caught.value.diagnostics["operation"]["phase"] == "finalize"
    assert "private-canary" not in str(caught.value) and "private-canary" not in json.dumps(
        caught.value.diagnostics
    )


@pytest.mark.parametrize("action", ["preflight", "collect"])
def test_fixed_remote_program_preserves_report_metadata_without_raw_command_output(
    tmp_path, monkeypatch, capsys, action
):
    root = tmp_path / "infra"
    state = root / ".deploy-state"
    state.mkdir(parents=True)
    marker = tmp_path / "env"
    marker.write_text("prod")
    for name in ("moly-backend", "moly-consumer"):
        (state / f"chat-content-{name}.json").write_text(json.dumps(runtime("production")))
    # A stale report must be removed before fresh candidate execution.
    (state / "daily-report-candidate.json").write_text(json.dumps({"stale": True}))
    config = {
        "action": action,
        "environment": "production",
        "sha": SHA,
        "tag": SHA,
        "infra_sha": INFRA,
        "image": "moly-backend",
    }
    expected = f"{w.ACCOUNT}.dkr.ecr.{w.REGION}.amazonaws.com/moly-backend:{SHA}"

    def run(argv, **kwargs):
        output = ""
        if argv[:3] == ["git", "remote", "get-url"]:
            output = "https://github.com/SOMA-OneTwoThree/moly-infra.git"
        elif argv[:2] == ["git", "rev-parse"]:
            output = INFRA
        elif argv[0] == "git":
            pass
        elif argv[:2] == ["bash", "deploy.sh"]:
            assert not (state / "daily-report-candidate.json").exists()
            (state / "daily-report-candidate.json").write_text(json.dumps(failure()))
            return SimpleNamespace(returncode=1, stdout="private-canary", stderr="private-canary")
        elif argv[:2] == ["docker", "ps"]:
            output = "moly-consumer"
        elif argv[:2] == ["docker", "inspect"]:
            output = "true" if "{{.State.Running}}" in argv else expected
        elif "scripts/check_chat_content_crypto.py" in argv:
            output = json.dumps(runtime("production"))
        elif "scripts/check_daily_report.py" in argv:
            return SimpleNamespace(
                returncode=1, stdout=json.dumps(failure()), stderr="private-canary"
            )
        elif argv[:2] == ["docker", "exec"]:
            pass  # schema check
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=output, stderr="private-canary")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.chdir(tmp_path)
    source = (
        w.REMOTE.replace("CONFIGURATION", repr(config), 1)
        .replace("Path('/root/moly-infra')", f"Path({str(root)!r})")
        .replace("Path('/etc/moly-env')", f"Path({str(marker)!r})")
    )
    with pytest.raises(SystemExit):
        exec(compile(source, "fixed-report-failure", "exec"), {})
    raw = capsys.readouterr().out
    result = json.loads(raw)
    assert result["reason"] == "daily_report_failed" and result["stage"] == "daily_report"
    assert result["daily_report"] == failure()
    assert "private-canary" not in raw
