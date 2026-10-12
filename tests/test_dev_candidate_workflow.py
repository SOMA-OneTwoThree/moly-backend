"""Offline controls for the trusted development candidate deployment boundary."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dev_candidate", ROOT / ".github/scripts/dev_candidate.py")
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
BASE, CANDIDATE, OLD = "a" * 40, "b" * 40, "c" * 40
INSTANCE = "i-1234567890abcdef0"


@pytest.mark.parametrize("mode,candidate,image,verify", [
    ("existing", "", "", "false"), ("existing", "", "dev-" + OLD, "false"),
    ("existing", "", "", "true"), ("inspect", "", "", "false"),
    ("candidate", CANDIDATE, "", "false"),
])
def test_compatible_inputs(mode, candidate, image, verify):
    control.validate_inputs("refs/heads/dev", mode, candidate, image, verify)


@pytest.mark.parametrize("ref,mode,candidate,image,verify", [
    ("refs/heads/feature", "candidate", CANDIDATE, "", "false"),
    ("refs/heads/main", "existing", "", "", "false"),
    ("refs/heads/dev", "candidate", "", "", "false"),
    ("refs/heads/dev", "candidate", "b" * 39, "", "false"),
    ("refs/heads/dev", "candidate", "B" * 40, "", "false"),
    ("refs/heads/dev", "candidate", CANDIDATE + "\necho unsafe", "", "false"),
    ("refs/heads/dev", "candidate", CANDIDATE, "dev-" + OLD, "false"),
    ("refs/heads/dev", "candidate", CANDIDATE, "", "true"),
    ("refs/heads/dev", "inspect", "", "dev-" + OLD, "false"),
    ("refs/heads/dev", "inspect", "", "", "true"),
    ("refs/heads/dev", "existing", CANDIDATE, "", "false"),
    ("refs/heads/dev", "existing", "", "prod-tag", "false"),
    ("refs/heads/dev", "existing", "", "dev-" + OLD, "true"),
])
def test_unsafe_inputs_rejected_before_any_external_call(ref, mode, candidate, image, verify, monkeypatch):
    monkeypatch.setattr(control, "run", lambda *_args, **_kw: pytest.fail("external call before validation"))
    with pytest.raises(control.GuardError):
        control.validate_inputs(ref, mode, candidate, image, verify)


def test_source_requires_latest_dev_and_candidate_ancestry(monkeypatch):
    monkeypatch.setattr(control, "github_commit", lambda ref: BASE if ref == "dev" else CANDIDATE)
    calls = []

    def run(args):
        calls.append(args)
        return BASE if args[1] == "rev-parse" else ""

    monkeypatch.setattr(control, "run", run)
    assert control.verify_source(CANDIDATE) == BASE
    assert ["git", "merge-base", "--is-ancestor", BASE, CANDIDATE] in calls
    monkeypatch.setattr(control, "run", lambda args: OLD)
    with pytest.raises(control.GuardError, match="dev_changed"):
        control.verify_source(CANDIDATE)


def test_non_descendant_and_missing_repository_commit_fail(monkeypatch):
    monkeypatch.setattr(control, "github_commit", lambda ref: BASE if ref == "dev" else CANDIDATE)

    def run(args):
        if args[1] == "merge-base":
            raise control.GuardError("command_failed")
        return BASE

    monkeypatch.setattr(control, "run", run)
    with pytest.raises(control.GuardError):
        control.verify_source(CANDIDATE)
    monkeypatch.setattr(control, "github_commit", lambda ref: BASE)
    with pytest.raises(control.GuardError, match="candidate_not_in_repository"):
        control.verify_source(CANDIDATE)


def test_dev_drift_fails(monkeypatch):
    monkeypatch.setattr(control, "github_commit", lambda ref: OLD)
    with pytest.raises(control.GuardError, match="dev_changed"):
        control.verify_drift(BASE)


@pytest.mark.parametrize("running", [[], [INSTANCE, "i-1234567890abcdef1"]])
def test_target_count_stops_before_host_or_build(monkeypatch, running):
    monkeypatch.setattr(control, "discover", lambda: ({"running": len(running)}, running))
    monkeypatch.setattr(control, "health", lambda _: {"status": "ok", "version": OLD})
    monkeypatch.setattr(control, "host_probe", lambda _: pytest.fail("host probe must not run"))
    with pytest.raises(control.GuardError, match="exactly_one"):
        control.inspect(True)
    control.inspect(False)


def test_mismatched_public_host_and_missing_rollback_fail(monkeypatch):
    monkeypatch.setattr(control, "discover", lambda: ({"running": 1}, [INSTANCE]))
    monkeypatch.setattr(control, "health", lambda _: {"status": "ok", "version": OLD})
    monkeypatch.setattr(control, "host_probe", lambda _: {"version": BASE})
    monkeypatch.setattr(control, "require_image", lambda _: pytest.fail("image lookup before host match"))
    with pytest.raises(control.GuardError, match="sha_mismatch"):
        control.inspect(True)
    monkeypatch.setattr(control, "host_probe", lambda _: {"version": OLD})
    monkeypatch.setattr(control, "aws", lambda *_: {"imageDetails": []})
    monkeypatch.setattr(control, "require_image", ORIGINAL_REQUIRE_IMAGE)
    with pytest.raises(control.GuardError, match="rollback_image_missing"):
        control.inspect(True)


ORIGINAL_REQUIRE_IMAGE = control.require_image


def test_probe_marker_precedes_docker_and_filters_extra_output(monkeypatch, capsys):
    captured = []
    value = {"version": OLD, "ready": True, "development": True,
             "flags": {key: True for key in control.FLAGS}, "secret": "never-display"}

    def send(instance, command):
        captured.append(command)
        return {"StandardOutputContent": json.dumps(value), "StandardErrorContent": "never-display"}

    monkeypatch.setattr(control, "send_command", send)
    result = control.host_probe(INSTANCE)
    assert captured[0].index(control.MARKER_GUARD) < captured[0].index("docker exec")
    assert "secret" not in result
    assert "never-display" not in capsys.readouterr().out
    value["flags"][control.FLAGS[0]] = "true"
    with pytest.raises(control.GuardError, match="host_flags_invalid"):
        control.host_probe(INSTANCE)


@pytest.mark.parametrize("marker", ["prod", "", "devx"])
def test_non_dev_marker_exits_before_following_command(tmp_path, marker):
    path = tmp_path / "marker"
    path.write_text(marker)
    guard = control.MARKER_GUARD.replace("/etc/moly-env", str(path))
    result = subprocess.run(["bash", "-c", guard + "\nprintf SHOULD_NOT_RUN"], capture_output=True, text=True)
    assert result.returncode == 41
    assert result.stdout.strip() == "DEV_MARKER_REQUIRED"


def test_deploy_rechecks_target_before_any_mutating_command(monkeypatch):
    monkeypatch.setattr(control, "require_image", lambda _: None)
    monkeypatch.setattr(control, "discover", lambda: ({"running": 1}, ["i-1234567890abcdef1"]))
    monkeypatch.setattr(control, "send_command", lambda *_args, **_kw: pytest.fail("must not deploy"))
    with pytest.raises(control.GuardError, match="dev_target_changed"):
        control.deploy(CANDIDATE, INSTANCE, "dev-" + OLD)


@pytest.mark.parametrize("ready", [True, False])
def test_success_requires_new_sha_and_host_readiness(monkeypatch, capsys, ready):
    monkeypatch.setattr(control, "require_image", lambda _: None)
    monkeypatch.setattr(control, "discover", lambda: ({"running": 1}, [INSTANCE]))
    monkeypatch.setattr(control, "send_command", lambda *_a, **_kw: {})
    monkeypatch.setattr(control.time, "sleep", lambda _: None)
    public = iter([OLD] + [CANDIDATE] * 6)
    monkeypatch.setattr(control, "health", lambda _: {"version": next(public)})
    calls = []

    def probe(instance):
        calls.append(instance)
        if len(calls) == 1:
            return {"version": OLD}
        if not ready:
            raise control.GuardError("host_not_ready_dev")
        return {"version": CANDIDATE, "ready": True, "development": True,
                "flags": {key: True for key in control.FLAGS}}

    monkeypatch.setattr(control, "host_probe", probe)
    if ready:
        control.deploy(CANDIDATE, INSTANCE, "dev-" + OLD)
        result = json.loads(capsys.readouterr().out)
        assert result["host"]["ready"] is True and result["host"]["version"] == CANDIDATE
    else:
        with pytest.raises(control.GuardError, match="readiness_not_observed"):
            control.deploy(CANDIDATE, INSTANCE, "dev-" + OLD)
        assert len(calls) == 7
        assert "deployed_sha" not in capsys.readouterr().out


def test_remote_failure_never_returns_raw_stdout_stderr(monkeypatch, capsys):
    calls = iter([{"Command": {"CommandId": "a" * 8 + "-" + "b" * 27}},
                  {"Status": "Failed", "StandardOutputContent": "privatebody",
                   "StandardErrorContent": "privatebody"}])
    monkeypatch.setattr(control, "aws", lambda *_: next(calls))
    monkeypatch.setattr(control.time, "sleep", lambda _: None)
    with pytest.raises(control.GuardError, match="remote_command_failed") as error:
        control.send_command(INSTANCE, control.MARKER_GUARD)
    assert "privatebody" not in str(error.value)
    assert "privatebody" not in str(capsys.readouterr())


@pytest.mark.parametrize("status,code,expected", [
    ("Failed", 41, "status=Failed exit=41"),
    ("Failed", 42, "status=Failed exit=42"),
    ("Failed", 43, "status=Failed exit=43"),
    ("Failed", 44, "status=Failed exit=44"),
    ("Failed", 45, "status=Failed exit=45"),
    ("Failed", 125, "status=Failed exit=125"),
    ("Cancelled", 126, "status=Cancelled exit=126"),
    ("TimedOut", 127, "status=TimedOut exit=127"),
    ("Cancelling", 1, "status=Cancelling exit=unknown"),
    ("Failed", "41", "status=Failed exit=unknown"),
    ("Failed", True, "status=Failed exit=unknown"),
    ("private-status", -1, "status=unknown exit=unknown"),
    (None, None, "status=unknown exit=unknown"),
    ({"private-status": True}, {}, "status=unknown exit=unknown"),
])
def test_failure_metadata_only_allowlisted(status, code, expected):
    class MetadataOnly(dict):
        def get(self, key, *args):
            assert key not in {"StandardOutputContent", "StandardErrorContent"}
            return super().get(key, *args)

    state = MetadataOnly(Status=status, ResponseCode=code,
                         StandardOutputContent="privatebody", StandardErrorContent="privatebody")
    result = control.remote_failure_code(state)
    assert result == "remote_command_failed " + expected
    assert "private" not in result


def test_probe_config_import_uses_fixed_phase_code():
    assert control.PROBE_PYTHON.index("try:") < control.PROBE_PYTHON.index("from app.config import settings")
    compile(control.PROBE_PYTHON, "synthetic-probe", "exec")
    assert "raise SystemExit(phase)" in control.PROBE_PYTHON


@pytest.mark.parametrize("failure_stage", [42, 43, 44, 45])
def test_probe_failure_stage_never_prints_exception_or_response(monkeypatch, capsys, failure_stage):
    import builtins
    import io
    from types import ModuleType
    import urllib.request

    imported = builtins.__import__
    settings = SimpleNamespace(environment="development", **{key: True for key in control.FLAGS})
    if failure_stage == 45:
        delattr(settings, control.FLAGS[0])
    fake_config = ModuleType("app.config")
    fake_config.settings = settings

    def fake_import(name, *args, **kwargs):
        if name == "app.config":
            if failure_stage == 42:
                raise ValueError("private-config-value")
            return fake_config
        return imported(name, *args, **kwargs)

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, url, timeout):
            if (failure_stage == 43 and url.endswith("/health")) or (
                failure_stage == 44 and url.endswith("/ready")
            ):
                raise ValueError("private-http-response")
            return Response(json.dumps({"version": OLD}).encode())

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: Opener())
    with pytest.raises(SystemExit) as error:
        exec(compile(control.PROBE_PYTHON, "synthetic-probe", "exec"), {})
    assert error.value.code == failure_stage
    assert capsys.readouterr().out == "PROBE_FAILED\n"


def test_subprocess_failure_redacted(monkeypatch):
    monkeypatch.setattr(control.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=1, stdout="privatebody", stderr="privatebody"))
    with pytest.raises(control.GuardError, match="command_failed") as error:
        control.run(["ignored"])
    assert "privatebody" not in str(error.value)


def test_invalid_public_sha_rejected(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _bound):
            return b'{"status":"ok","version":"invalid","secret":"privatebody"}'

    monkeypatch.setattr(control, "open_health", lambda *_args, **_kw: Response())
    with pytest.raises(control.GuardError, match="public_health_invalid"):
        control.health("https://invalid.invalid")


def test_local_default_aws_and_health_redirect_are_not_available(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(control, "run", lambda *_a, **_kw: pytest.fail("must not use default AWS"))
    with pytest.raises(control.GuardError, match="project_workflow_required"):
        control.aws("ec2", "describe-instances")
    with pytest.raises(control.GuardError, match="fixed_dev_health_required"):
        control.open_health("https://moly.asia/health")
    assert control.NoRedirect().redirect_request(None, None, None, None, None, None) is None


def test_workflow_trusted_controls_and_compatibility():
    document = yaml.safe_load((ROOT / ".github/workflows/deploy-dev.yml").read_text())
    event = document.get("on", document.get(True))
    assert event["workflow_dispatch"]["inputs"]["mode"]["default"] == "existing"
    assert event["push"]["branches"] == ["dev"]
    assert "app/**" not in event["push"]["paths-ignore"]
    jobs = document["jobs"]
    assert "id-token" not in jobs["validate-inputs"]["permissions"]
    for job in ("build-and-deploy", "verify-subscription", "candidate-control"):
        assert jobs[job]["needs"] == "validate-inputs"
    steps = jobs["candidate-control"]["steps"]
    names = [step["name"] for step in steps]
    assert names.index("Inspect development target before any build") < names.index("Build and push verified candidate")
    candidate = next(s for s in steps if s["name"] == "Checkout candidate separately from trusted controls")
    assert candidate["with"]["path"] == "candidate-src"
    assert candidate["with"]["persist-credentials"] is False
    build = next(s for s in steps if s["name"] == "Build and push verified candidate")
    assert build["with"]["context"] == "./candidate-src"
    assert not ({"secrets", "secret-envs", "ssh", "allow"} & build["with"].keys())
    for step in steps:
        if "run" in step:
            assert "candidate-src" not in step["run"]
    assert document["concurrency"]["group"] == "deploy-moly-backend-dev"
