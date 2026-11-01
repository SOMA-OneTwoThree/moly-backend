"""The report check must be shipped and executed, not skipped on fresh builds."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import textwrap

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SHA = "a"*40


@pytest.mark.parametrize("fresh,missing,expected", [(True, False, 0), (True, True, 105),
                                                   (False, True, 0)])
def test_deployed_dev_probe_requires_the_packaged_script(tmp_path, fresh, missing, expected):
    workflow = yaml.safe_load((ROOT/".github/workflows/deploy-dev.yml").read_text())
    steps = workflow["jobs"]["build-and-deploy"]["steps"]
    shell = next(s["run"] for s in steps if s["name"] == "Deploy via SSM (single dev instance)")
    python = textwrap.dedent(shell.split("<<'PYTHON'", 1)[1].split("\nPYTHON", 1)[0])
    params = tmp_path/"params.json"
    env = dict(os.environ, DEPLOY_TAG="dev-"+SHA, VERIFY_DAILY_REPORT="true" if fresh else "false")
    subprocess.run([sys.executable, "-c", python, str(params)], env=env, check=True)
    command = json.loads(params.read_text())["commands"][0]
    start = command.index('if [ "'+env["VERIFY_DAILY_REPORT"]+'" = true ]; then')
    probe = command[start:command.index("\nfi", start)+3]
    copied = set()
    for line in (ROOT/"Dockerfile").read_text().splitlines():
        if line.startswith("COPY "):
            copied.update(shlex.split(line)[1:-1])
    if missing:
        copied.discard("scripts/check_daily_report.py")
    for name in copied:
        if name.startswith("scripts/"):
            destination = tmp_path/name
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes((ROOT/name).read_bytes())
    # Only the docker exec file lookup is simulated; the workflow's actual
    # generated shell, error propagation, and Dockerfile COPY list are used.
    docker = tmp_path/"docker"
    docker.write_text("#!/bin/sh\ntest -f \"$4\"\n")
    docker.chmod(0o755)
    result = subprocess.run(["sh", "-ec", probe], cwd=tmp_path,
                            env={**env, "PATH": str(tmp_path)+":"+env["PATH"]})
    assert result.returncode == expected


def test_production_probe_does_not_silently_skip_a_missing_script():
    workflow = yaml.safe_load((ROOT/".github/workflows/deploy.yml").read_text())
    shell = next(s["run"] for s in workflow["jobs"]["build-and-deploy"]["steps"]
                 if s["name"] == "Rolling deploy via SSM")
    probe = shell[shell.index('if [ "$VERIFY_DAILY_REPORT" = true ]; then'):]
    assert "docker exec moly-backend python scripts/check_daily_report.py" in probe
    assert "if [ -f scripts/check_daily_report.py ]" not in probe
