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
    env = dict(os.environ, DEPLOY_TAG="dev-"+SHA, DEV_INFRA_SHA="b"*40, VERIFY_DAILY_REPORT="true" if fresh else "false")
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


def test_encryption_deploy_uses_reviewed_infra_without_key_provisioning():
    source=(ROOT/".github/workflows/deploy-dev.yml").read_text()
    assert "provision" not in source
    assert "MOLY_DEV_CHAT_CONTENT_KEYRING" not in source
    assert 'git checkout --detach {infra}' in source
    assert 'git rev-parse FETCH_HEAD' in source
    assert 'REVIEWED_DEV_INFRA_SHA_REQUIRED' in source
    assert '"executionTimeout": ["1200"]' in source
    assert 'seq 1 120' in source


def _generated_dev_command(tmp_path, preflight):
    workflow=yaml.safe_load((ROOT/".github/workflows/deploy-dev.yml").read_text())
    steps=workflow['jobs']['build-and-deploy']['steps']
    shell=next(s['run'] for s in steps if s['name']=='Deploy via SSM (single dev instance)')
    python=textwrap.dedent(shell.split("<<'PYTHON'",1)[1].split('\nPYTHON',1)[0])
    params=tmp_path/'generated.json'
    subprocess.run([sys.executable,'-c',python,str(params)],check=True,env=dict(os.environ,
        DEPLOY_TAG='dev-'+SHA,DEV_INFRA_SHA='b'*40,VERIFY_DAILY_REPORT='true',PREFLIGHT_ONLY=preflight))
    return json.loads(params.read_text())['commands'][0],steps


@pytest.mark.parametrize('message,expected',[
    ('success',0),
    ('ERROR: chat_content_keyring_invalid',187),
    ('ERROR: chat content crypto key/capability preflight failed',182),
    ('ERROR: chat content crypto candidate proof invalid',183),
    ('ERROR: chat content encryption downgrade floor rejected candidate',184),
    ('==> DB schema preflight',185),
    ('unknown',186),
])
def test_dev_diagnostic_generated_command_never_deploys_or_exposes_logs(tmp_path,message,expected):
    command,steps=_generated_dev_command(tmp_path,'true')
    assert 'check_dev post' not in command
    assert 'activate_chat_content_crypto_dev.py' not in command
    assert 'check_daily_report.py' not in command
    assert 'sudo bash deploy.sh' not in command
    assert 'sudo env DEPLOY_PREFLIGHT_ONLY=1 bash deploy.sh' in command
    assert next(s for s in steps if s['name']=='Verify via public endpoint')['if']=='${{ !inputs.preflight_only }}'
    diagnostic=command[command.index('umask 077'):]
    (tmp_path/'deploy.sh').write_text('PREFLIGHT_ONLY="${DEPLOY_PREFLIGHT_ONLY:-0}"\n')
    sudo=tmp_path/'sudo'
    sudo.write_text('#!/bin/sh\nprintf "%s\\n" "$DIAGNOSTIC_MESSAGE" "secret-log-canary"\n[ "$DIAGNOSTIC_MESSAGE" = success ]\n')
    sudo.chmod(0o755)
    aws=tmp_path/'aws'
    aws.write_text('#!/bin/sh\nprintf \'{bad-json\\n\'\n')
    aws.chmod(0o755)
    result=subprocess.run(['sh','-ec',diagnostic],cwd=tmp_path,capture_output=True,text=True,
        env=dict(os.environ,PATH=str(tmp_path)+':'+os.environ['PATH'],TMPDIR=str(tmp_path),DIAGNOSTIC_MESSAGE=message))
    assert result.returncode==expected
    assert result.stdout==result.stderr==''
    assert not list(tmp_path.glob('tmp.*'))


def test_dev_normal_command_keeps_deployment_and_activation(tmp_path):
    command,_=_generated_dev_command(tmp_path,'false')
    assert 'sudo bash deploy.sh' in command
    assert 'check_dev post' in command
    assert 'activate_chat_content_crypto_dev.py' in command
    assert 'diagnostic_log=' not in command


@pytest.mark.parametrize('raw,expected',[
    ('{"environment":"development","environment":"development","keys":{}}',187),
    ('{"environment":"production","keys":{"1":"secret-canary"}}',188),
    ('{"environment":"development","keys":{}}',189),
    ('{"environment":"development","keys":{"1":"secret-canary"}}',190),
    ('{"environment":"development","keys":{"1":"eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="}}',181),
])
def test_diagnostic_keyring_reason_is_fixed_and_secret_free(tmp_path,raw,expected):
    command,_=_generated_dev_command(tmp_path,'true')
    diagnostic=command[command.index('umask 077'):]
    (tmp_path/'deploy.sh').write_text('PREFLIGHT_ONLY="${DEPLOY_PREFLIGHT_ONLY:-0}"\n')
    (tmp_path/'key-input').write_text(raw)
    for name,source in [('sudo','printf "ERROR: chat_content_keyring_invalid\\n"; exit 1'),
                        ('aws','cat "$KEY_INPUT"')]:
        path=tmp_path/name
        path.write_text('#!/bin/sh\n'+source+'\n')
        path.chmod(0o755)
    result=subprocess.run(['sh','-ec',diagnostic],cwd=tmp_path,capture_output=True,text=True,
        env=dict(os.environ,PATH=str(tmp_path)+':'+os.environ['PATH'],TMPDIR=str(tmp_path),KEY_INPUT=str(tmp_path/'key-input')))
    assert result.returncode==expected
    assert result.stdout==result.stderr==''
    assert not list(tmp_path.glob('tmp.*'))


def test_generated_remote_scripts_do_not_contain_accidental_actions_expressions():
    workflow = yaml.safe_load((ROOT / '.github/workflows/deploy-dev.yml').read_text())
    for step in workflow['jobs']['build-and-deploy']['steps']:
        assert '${{' not in step.get('run', ''), 'Shell/f-string braces must not become Actions expressions'
