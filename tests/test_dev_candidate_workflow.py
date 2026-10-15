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
    ("inspect", "", "", "false"),
    ("candidate", CANDIDATE, "", "false"),
])
def test_compatible_inputs(mode, candidate, image, verify):
    control.validate_inputs("refs/heads/dev", mode, candidate, image, verify)


@pytest.mark.parametrize("ref,mode,candidate,image,verify", [
    ("refs/heads/dev", "existing", "", "", "true"),
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
    assert "verify-subscription" not in jobs
    assert "verify_subscription_only" not in event["workflow_dispatch"]["inputs"]
    for job in ("build-and-deploy", "candidate-control"):
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
    for step in jobs["build-and-deploy"]["steps"]:
        command = step.get("run", "")
        assert "StandardOutputContent" not in command
        assert "StandardErrorContent" not in command


def runtime_payload():
    import copy
    api = dict(git_sha=OLD, environment="development", context_checkpoint_enabled=True,
               current_turn_context_enabled=True, model_chat="gpt-6-luna",
               embedder_model="text-embedding-3-small", llm_max_tokens=1024,
               sdk=dict(control.RUNTIME_SDK))
    consumer = dict(git_sha=OLD, environment="development", context_checkpoint_enabled=True,
                    model_utility="gpt-6-luna", llm_timeout_s=60.0,
                    job_content_timeout_s=120.0, job_content_lease_s=150.0,
                    job_content_heartbeat_s=20.0, job_content_max_attempts=3,
                    job_finalize_timeout_s=5.0, sdk=dict(control.RUNTIME_SDK))
    identity = {"image": "sha256:" + "d" * 64, "running": True}
    return {"status": "available", "containers": {role: copy.deepcopy(identity) for role in ("api", "consumer")},
            "settings": {"api": api, "consumer": consumer}}


def mock_runtime_rechecks(monkeypatch):
    monkeypatch.setattr(control, "host_probe", lambda _: {
        "version": OLD, "ready": True, "development": True,
        "flags": {key: True for key in control.FLAGS}})
    monkeypatch.setattr(control, "health", lambda _: {"status": "ok", "version": OLD})


def test_runtime_probe_fixed_remote_program_and_success(monkeypatch, capsys):
    import contextlib
    import io
    import shlex
    payload = runtime_payload()
    docker_calls = []

    def docker(args, **kwargs):
        docker_calls.append(args)
        assert kwargs == {"capture_output": True, "text": True, "timeout": 10, "check": False}
        if args[:2] == ["docker", "inspect"]:
            assert args[2:4] == ["--format", '{"image":{{json .Image}},"running":{{json .State.Running}}}']
            role = {"moly-backend": "api", "moly-consumer": "consumer"}[args[4]]
            value = payload["containers"][role]
        else:
            assert args[:2] == ["docker", "exec"] and args[3:5] == ["python", "-c"]
            role = {"moly-backend": "api", "moly-consumer": "consumer"}[args[2]]
            assert "model_dump" not in args[5] and "os.environ" not in args[5]
            value = payload["settings"][role]
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="DO_NOT_SHOW")

    def send(instance, command, *, seconds):
        assert instance == INSTANCE and seconds == 90
        assert command.splitlines()[:2] == ["set -eu", control.MARKER_GUARD]
        assert ".Config.Env" not in command and "backend.env" not in command
        # shlex-quoted program contains physical newlines; split the complete shell suffix.
        parts = shlex.split(command.split("\n", 2)[2])
        assert parts[:2] == ["python3", "-c"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(parts[2], "runtime-probe", "exec"), {})
        assert "DO_NOT_SHOW" not in out.getvalue()
        return {"StandardOutputContent": out.getvalue()}

    monkeypatch.setattr(control.subprocess, "run", docker)
    monkeypatch.setattr(control, "send_command", send)
    mock_runtime_rechecks(monkeypatch)
    result = control.runtime_probe(INSTANCE, OLD)
    assert result["status"] == "available" and result["utility_provider"] == "openai"
    assert result["settings"] == payload["settings"]
    assert len(docker_calls) == 6  # before images, settings, after images
    assert "DO_NOT_SHOW" not in capsys.readouterr().out


@pytest.mark.parametrize("role,key,value,reason", [
    ("api", "model_chat", "sk-fake-private-value", "model_unapproved"),
    ("consumer", "model_utility", "claude-unknown", "model_unapproved"),
    ("api", "embedder_model", "https://secret.invalid/token", "model_unapproved"),
    ("api", "context_checkpoint_enabled", 1, "runtime_shape_invalid"),
    ("consumer", "context_checkpoint_enabled", "true", "runtime_shape_invalid"),
    ("api", "llm_max_tokens", True, "runtime_limits_invalid"),
    ("api", "llm_max_tokens", "1024", "runtime_limits_invalid"),
    ("api", "llm_max_tokens", 0, "runtime_limits_invalid"),
    ("consumer", "job_content_max_attempts", True, "runtime_limits_invalid"),
    ("consumer", "job_content_max_attempts", 3.0, "runtime_limits_invalid"),
    ("consumer", "job_content_heartbeat_s", float("nan"), "runtime_limits_invalid"),
    ("consumer", "job_content_lease_s", float("inf"), "runtime_limits_invalid"),
    ("consumer", "job_content_timeout_s", "120", "runtime_limits_invalid"),
    ("consumer", "llm_timeout_s", True, "runtime_limits_invalid"),
    ("consumer", "job_content_lease_s", 125, "runtime_limits_invalid"),
    ("consumer", "job_content_heartbeat_s", 21, "runtime_limits_invalid"),
    ("consumer", "llm_timeout_s", 121, "runtime_limits_invalid"),
    ("consumer", "git_sha", BASE, "identity_drift"),
    ("api", "environment", "production", "identity_drift"),
])
def test_runtime_values_fail_closed_without_raw_output(monkeypatch, capsys, role, key, value, reason):
    payload = runtime_payload()
    payload["settings"][role][key] = value
    monkeypatch.setattr(control, "send_command", lambda *_a, **_k: {"StandardOutputContent": json.dumps(payload)})
    monkeypatch.setattr(control, "host_probe", lambda _: pytest.fail("recheck before invalid values rejected"))
    assert control.runtime_probe(INSTANCE, OLD) == {"status": "unavailable", "reason": reason}
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mutate,reason", [
    (lambda p: p.update(secret="DO_NOT_SHOW"), "runtime_shape_invalid"),
    (lambda p: p["settings"]["api"].update(secret="DO_NOT_SHOW"), "runtime_shape_invalid"),
    (lambda p: p["settings"]["consumer"]["sdk"].update(openai="DO_NOT_SHOW"), "sdk_unapproved"),
    (lambda p: p["settings"]["api"]["sdk"].update(extra="DO_NOT_SHOW"), "sdk_unapproved"),
    (lambda p: p["containers"]["consumer"].update(image="sha256:" + "e" * 64), "identity_drift"),
    (lambda p: p["containers"]["consumer"].update(running=False), "container_unavailable"),
    (lambda p: p["containers"]["api"].update(running=1), "container_unavailable"),
    (lambda p: p["containers"].pop("consumer"), "runtime_shape_invalid"),
])
def test_runtime_extra_fields_sdk_and_container_drift(monkeypatch, capsys, mutate, reason):
    payload = runtime_payload()
    mutate(payload)
    monkeypatch.setattr(control, "send_command", lambda *_a, **_k: {
        "StandardOutputContent": json.dumps(payload), "StandardErrorContent": "DO_NOT_SHOW"})
    assert control.runtime_probe(INSTANCE, OLD) == {"status": "unavailable", "reason": reason}
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("raw", ["[]", "DO_NOT_SHOW", "x" * 8193,
    '{"status":"unavailable","reason":"DO_NOT_SHOW"}',
    '{"status":"unavailable","status":"available","reason":"identity_drift"}',
])
def test_runtime_malformed_bodies_are_not_exposed(monkeypatch, capsys, raw):
    monkeypatch.setattr(control, "send_command", lambda *_a, **_k: {"StandardOutputContent": raw})
    assert control.runtime_probe(INSTANCE, OLD) == {"status": "unavailable", "reason": "runtime_shape_invalid"}
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("field,value,reason", [
    ("model_utility", "sk-fake-secret", "model_unapproved"),
    ("sdk", {"openai": "sk-fake-secret", "anthropic": "0.116.0"}, "sdk_unapproved"),
])
def test_remote_validator_hides_child_failure_and_unapproved_model(monkeypatch, capsys, field, value, reason):
    import contextlib
    import io
    import shlex
    payload = runtime_payload()
    payload["settings"]["consumer"][field] = value

    def docker(args, **kwargs):
        if args[1] == "inspect":
            value = payload["containers"]["api"]
        else:
            value = payload["settings"]["api" if args[2] == "moly-backend" else "consumer"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="sk-fake-secret")

    def send(instance, command, **kwargs):
        program = shlex.split(command.split("\n", 2)[2])[2]
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            exec(compile(program, "remote-runtime", "exec"), {})
        assert "sk-fake-secret" not in captured.getvalue()
        return {"StandardOutputContent": captured.getvalue()}

    monkeypatch.setattr(control.subprocess, "run", docker)
    monkeypatch.setattr(control, "send_command", send)
    assert control.runtime_probe(INSTANCE, OLD)["reason"] == reason
    monkeypatch.setattr(control.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=1, stdout="sk-fake-secret", stderr="sk-fake-secret"))
    assert control.runtime_probe(INSTANCE, OLD)["reason"] == "container_unavailable"
    assert capsys.readouterr().out == ""


def test_runtime_collection_rejects_container_replacement_during_snapshot(monkeypatch):
    payload = runtime_payload()
    inspections = 0

    def docker(args, **_kwargs):
        nonlocal inspections
        if args[1] == "inspect":
            inspections += 1
            value = dict(payload["containers"]["api"])
            if inspections > 2:
                value["image"] = "sha256:" + "e" * 64
        else:
            value = payload["settings"]["api" if args[2] == "moly-backend" else "consumer"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    monkeypatch.setattr(control.subprocess, "run", docker)
    with pytest.raises(ValueError, match="^identity_drift$"):
        control._runtime_collect()


def test_runtime_remote_exception_is_fixed_and_never_exposes_message(monkeypatch, capsys):
    def failed_send(*_args, **_kwargs):
        raise RuntimeError("sk-fake-secret https://hooks.invalid/private")

    monkeypatch.setattr(control, "send_command", failed_send)
    assert control.runtime_probe(INSTANCE, OLD) == {"status": "unavailable", "reason": "runtime_unavailable"}
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("change", ["host", "public", "flags"])
def test_runtime_rechecks_after_snapshot(monkeypatch, change):
    payload = runtime_payload()
    monkeypatch.setattr(control, "send_command", lambda *_a, **_k: {"StandardOutputContent": json.dumps(payload)})
    mock_runtime_rechecks(monkeypatch)
    if change == "public":
        monkeypatch.setattr(control, "health", lambda _: {"version": BASE})
    else:
        monkeypatch.setattr(control, "host_probe", lambda _: {
            "version": BASE if change == "host" else OLD,
            "flags": {key: change != "flags" for key in control.FLAGS}})
    assert control.runtime_probe(INSTANCE, OLD) == {"status": "unavailable", "reason": "identity_drift"}


def test_new_diagnostics_only_run_for_explicit_inspect(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(control, "discover", lambda: ({"running": 1}, [INSTANCE]))
    mock_runtime_rechecks(monkeypatch)
    monkeypatch.setattr(control, "require_image", lambda _: None)
    monkeypatch.setattr(control, "output", lambda **_: None)
    monkeypatch.setattr(control, "runtime_probe", lambda *a: calls.append(a) or {"status": "available"})
    control.inspect(True)
    assert calls == []
    control.inspect(False)
    assert calls == [(INSTANCE, OLD)]
    monkeypatch.setattr(control, "health", lambda _: {"version": BASE})
    calls.clear()
    control.inspect(False)
    assert calls == []
    assert '"reason": "identity_drift"' in capsys.readouterr().out


def test_runtime_sdk_contract_matches_lock_and_inspect_files_do_not_autodeploy():
    import tomllib
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {p["name"]: p["version"] for p in lock["package"]}
    assert {key: versions[key] for key in control.RUNTIME_SDK} == control.RUNTIME_SDK
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy-dev.yml").read_text())
    event = workflow.get("on", workflow.get(True))
    ignored = event["push"]["paths-ignore"]
    assert ".github/scripts/dev_candidate.py" in ignored
    assert "tests/test_dev_candidate_workflow.py" in ignored and "docs/**" in ignored
