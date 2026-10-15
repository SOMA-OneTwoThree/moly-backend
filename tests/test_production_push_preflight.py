"""Offline guards for the existing production deploy workflow; no AWS execution."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess

import pytest

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/deploy.yml"
TEXT = WORKFLOW.read_text()
START = TEXT.index("      - name: Rolling deploy via SSM")
RUN_START = TEXT.index("        run: |\n", START) + len("        run: |\n")
RUN_END = TEXT.index("\n      # 7.", RUN_START)
SHELL = "\n".join(line[10:] for line in TEXT[RUN_START:RUN_END].splitlines())
SHA = "42b57fd108c7ff128819d08d406f67c4f450f42e"


def function(name: str) -> str:
    start = SHELL.index(name + "() {")
    return SHELL[start:SHELL.index("\n}\n", start) + 3]


def bash(code: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + code], capture_output=True, text=True,
        env=dict(os.environ, **env), timeout=5, check=False,
    )


def test_rolling_shell_syntax():
    result = subprocess.run(["bash", "-n"], input=SHELL, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_push_checks_before_service_replacement_and_post_version_check():
    assert "github.event_name == 'push' || (github.event_name == 'workflow_dispatch' && inputs.verify_morning_push)" in TEXT
    assert "PREVIOUS_SHA: ${{ github.event.before }}" in TEXT
    start = SHELL.index('# Push-only checks')
    guard = SHELL[start:SHELL.index('if [ "$VERIFY_MORNING_PUSH"', start)]
    assert 'if [ "$DEPLOY_EVENT" = push ]; then' in guard
    assert 'verify_runtime_version "$PREVIOUS_SHA"' in guard
    assert "verify_worker_timers" in guard
    assert SHELL.index(guard) < SHELL.index("DEPLOY_PREFLIGHT_ONLY=1") < SHELL.index("aws elbv2 deregister-targets")
    assert 'if [ "$DEPLOY_EVENT" = push ]; then\n  verify_runtime_version "$DEPLOY_TAG"\nfi' in SHELL
    assert "inputs.image_tag || github.sha" in TEXT
    assert "inputs.expected_instances || '2'" in TEXT
    assert "inputs.verify_morning_push" in TEXT
    assert "asyncio.wait_for(push.prepare_access_token(), timeout=15)" in TEXT
    assert 'assert settings.morning_push_enabled is True' in TEXT


@pytest.mark.parametrize("value", ["", "0" * 40, "A" * 40, "a" * 39, "a" * 40 + ";printf BAD"])
def test_invalid_previous_sha_fails_without_cloud_call(value):
    start = SHELL.index('if ! [[ "$PREVIOUS_SHA"')
    guard = SHELL[start:SHELL.index("\n  fi", start) + len("\n  fi")]
    result = bash(guard, PREVIOUS_SHA=value)
    assert result.returncode != 0
    assert "BAD" not in result.stdout


def test_runtime_parameters_are_fixed_json_and_valid_python():
    result = bash(function("runtime_parameters") + '\nruntime_parameters "$EXPECTED"', EXPECTED=SHA)
    assert result.returncode == 0, result.stderr
    params = json.loads(result.stdout)
    assert set(params) == {"commands"} and len(params["commands"]) == 1
    command = params["commands"][0]
    source = command.split("\n", 1)[1].rsplit("\nMOLY_OLD_RUNTIME", 1)[0]
    tree = ast.parse(source)
    assert tree
    assert SHA in source and "moly-backend" in source and "moly-consumer" in source
    assert "capture_output=True" in source and "'python','-B'" in source
    assert "s.git_sha==" in source and "s.morning_push_enabled is True" in source
    assert "send(" not in source and "get-parameter" not in source


@pytest.mark.parametrize("states,expected", [
    ("active inactive", 0), ("inactive inactive", 1), ("active active", 1),
    ("failed inactive", 1), ("unknown inactive", 1), ("activating inactive", 1),
])
def test_shared_timer_state_is_fail_closed(states, expected):
    mock = r'''
aws() {
  case "$*" in
    *send-command*) printf 'fixed-command' ;;
    *get-command-invocation*)
      local value
      if [[ "$*" == *'--instance-id one '* ]]; then value=${HOST_STATES%% *}; else value=${HOST_STATES##* }; fi
      printf '%s' "$value" ;;
    *) return 99 ;;
  esac
}
poll_ssm() { return 0; }
INSTANCE_IDS='one two'
AWS_REGION=fixed
'''
    result = bash(mock + function("verify_worker_timers") + "\nverify_worker_timers", HOST_STATES=states)
    assert (result.returncode == 0) == (expected == 0), result.stderr
    # Type=oneshot service may be activating during one normal tick; only timer is queried.
    assert "moly-worker.service" not in function("verify_worker_timers")


@pytest.mark.parametrize("rows", [
    [{"Target": {"Id": "one"}, "TargetHealth": {"State": "healthy"}}, {"Target": {"Id": "two"}, "TargetHealth": {"State": "healthy"}}],
    [{"Target": {"Id": "one"}, "TargetHealth": {"State": "unhealthy"}}, {"Target": {"Id": "two"}, "TargetHealth": {"State": "healthy"}}],
    [{"Target": {"Id": "one"}, "TargetHealth": {"State": "healthy"}}],
])
def test_target_health_matches_exact_old_hosts(rows):
    line = next(line for line in SHELL.splitlines() if 'TG_SNAPSHOT="$TG_SNAPSHOT" python3 -c' in line)
    result = bash(line, INSTANCE_IDS="one two", TG_SNAPSHOT=json.dumps({"TargetHealthDescriptions": rows}))
    assert (result.returncode == 0) == (len(rows) == 2 and all(x["TargetHealth"]["State"] == "healthy" for x in rows))


@pytest.mark.parametrize("healthy", [True, False])
def test_embedded_runtime_python_executes_with_only_mocked_dependencies(monkeypatch, capsys, healthy):
    from types import SimpleNamespace
    import urllib.request

    result = bash(function("runtime_parameters") + '\nruntime_parameters "$EXPECTED"', EXPECTED=SHA)
    command = json.loads(result.stdout)["commands"][0]
    source = command.split("\n", 1)[1].rsplit("\nMOLY_OLD_RUNTIME", 1)[0]

    def fake_run(args, **kwargs):
        assert args[:2] in (["docker", "inspect"], ["docker", "exec"])
        assert kwargs["capture_output"] is True
        if args[:2] == ["docker", "exec"]:
            compile(args[-1], "config-probe", "exec")
        return SimpleNamespace(stdout="true", returncode=0)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return json.dumps({"db": "ok", "version": SHA if healthy else "0" * 40}).encode()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: Response())
    if healthy:
        exec(compile(source, "runtime-probe", "exec"), {})
        assert json.loads(capsys.readouterr().out) == {"runtime_healthy": True}
    else:
        with pytest.raises(SystemExit):
            exec(compile(source, "runtime-probe", "exec"), {})
        assert capsys.readouterr().out.strip() == "runtime_check_failed"
