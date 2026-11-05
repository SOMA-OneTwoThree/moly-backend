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


def _generated_dev_command(tmp_path, preflight, benchmark="false"):
    workflow=yaml.safe_load((ROOT/".github/workflows/deploy-dev.yml").read_text())
    steps=workflow['jobs']['build-and-deploy']['steps']
    shell=next(s['run'] for s in steps if s['name']=='Deploy via SSM (single dev instance)')
    python=textwrap.dedent(shell.split("<<'PYTHON'",1)[1].split('\nPYTHON',1)[0])
    params=tmp_path/'generated.json'
    subprocess.run([sys.executable,'-c',python,str(params)],check=True,env=dict(os.environ,
        DEPLOY_TAG='dev-'+SHA,DEV_INFRA_SHA='b'*40,VERIFY_DAILY_REPORT='true',PREFLIGHT_ONLY=preflight,CRYPTO_BENCHMARK=benchmark))
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


def _benchmark_parser():
    workflow=yaml.safe_load((ROOT/'.github/workflows/deploy-dev.yml').read_text())
    shell=next(s['run'] for s in workflow['jobs']['build-and-deploy']['steps']
               if s['name']=='Deploy via SSM (single dev instance)')
    return textwrap.dedent(shell.split("<<'BENCHMARK_OUTPUT'",1)[1].split('\nBENCHMARK_OUTPUT',1)[0])


def _benchmark_metrics():
    result={'samples':100,'warmup_samples':10,'window_messages':120,'message_chars':2000,'storage_helpers_only':1}
    for name in ('encrypted_turn','plaintext_turn','added_turn','encrypted_window_decode','plaintext_window_read','added_window_read'):
        result[name+'_median_ms']=.2
        result[name+'_p95_ms']=.4
    return result


def test_crypto_benchmark_requires_diagnostic_and_preserves_normal_generated_command(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        _generated_dev_command(tmp_path,'false','true')
    diagnostic,_=_generated_dev_command(tmp_path,'true','true')
    assert 'CHAT_CRYPTO_BENCHMARK ' in diagnostic
    assert 'docker exec moly-backend python -c' in diagnostic[diagnostic.index('umask 077'):]
    assert 'check_dev post' not in diagnostic and 'activate_chat_content_crypto_dev.py' not in diagnostic
    regular,_=_generated_dev_command(tmp_path,'false','false')
    assert 'CHAT_CRYPTO_BENCHMARK ' not in regular
    preflight,_=_generated_dev_command(tmp_path,'true','false')
    assert 'CHAT_CRYPTO_BENCHMARK ' not in preflight


def test_generated_crypto_benchmark_runs_only_synthetic_local_codec_and_roundtrips(tmp_path):
    import base64
    command,_=_generated_dev_command(tmp_path,'true','true')
    tail=command[command.index('umask 077'):]
    tokens=shlex.split(tail[tail.index('docker exec moly-backend python -c '):])
    source=tokens[5]
    compile(source,'fixed-benchmark','exec')
    assert all(forbidden not in source for forbidden in ('asyncpg','get_sessionmaker','load_mode(', 'httpx', 'boto3'))
    env=dict(os.environ,ENVIRONMENT='development',GIT_SHA=SHA,
             CHAT_CONTENT_ENCRYPTION_KEYS=json.dumps({'environment':'development',
                 'keys':{'1':base64.b64encode(b'x'*32).decode()}}),
             SUPABASE_DB_CONNECTION_STRING='postgresql://test:test@127.0.0.1:1/test')
    result=subprocess.run([sys.executable,'-c',source,SHA],cwd=ROOT,env=env,
                          text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert result.stderr==''
    assert result.stdout.startswith('CHAT_CRYPTO_BENCHMARK ')
    value=json.loads(result.stdout.split(' ',1)[1])
    assert set(value)==set(_benchmark_metrics())
    assert value['samples']==100 and value['window_messages']==120 and value['message_chars']==2000
    assert all(type(v) in (int,float) for v in value.values())


def test_benchmark_output_parser_exposes_only_numeric_allowlist(tmp_path):
    metrics=_benchmark_metrics()
    path=tmp_path/'private-output'
    path.write_text('unrelated-secret-canary\nCHAT_CRYPTO_BENCHMARK '+json.dumps(metrics)+'\n')
    result=subprocess.run([sys.executable,'-c',_benchmark_parser(),str(path)],text=True,capture_output=True)
    assert result.returncode==0 and result.stderr==''
    assert 'canary' not in result.stdout
    assert json.loads(result.stdout.split(' ',1)[1])==metrics


@pytest.mark.parametrize('mutation', ['missing','extra','string','bool','nan','negative','wrong_size',
                                     'p95','duplicate_line','duplicate_key'])
def test_benchmark_output_parser_rejects_untrusted_or_nonfinite_metadata(tmp_path,mutation):
    metrics=_benchmark_metrics()
    if mutation=='extra':
        metrics['secret']='secret-canary'
    elif mutation in {'string','bool','nan','negative'}:
        metrics['encrypted_turn_median_ms']={'string':'secret-canary','bool':True,'nan':float('nan'),'negative':-1}[mutation]
    elif mutation=='wrong_size':
        metrics['samples']=99
    elif mutation=='p95':
        metrics['encrypted_turn_p95_ms']=0
    line='CHAT_CRYPTO_BENCHMARK '+json.dumps(metrics)
    if mutation=='missing':
        line='unrelated-secret-canary'
    elif mutation=='duplicate_line':
        line+='\n'+line
    elif mutation=='duplicate_key':
        line=line.replace('"samples": 100','"samples": 100, "samples": 100')
    path=tmp_path/'private-output'
    path.write_text(line)
    result=subprocess.run([sys.executable,'-c',_benchmark_parser(),str(path)],text=True,capture_output=True)
    assert result.returncode!=0 and result.stdout==''
    assert result.stderr.strip()=='DEV_CRYPTO_BENCHMARK_OUTPUT_INVALID'


def test_ssm_poll_benchmark_sanitizes_output_and_removes_private_capture(tmp_path):
    workflow=yaml.safe_load((ROOT/'.github/workflows/deploy-dev.yml').read_text())
    shell=next(s['run'] for s in workflow['jobs']['build-and-deploy']['steps']
               if s['name']=='Deploy via SSM (single dev instance)')
    function=shell[shell.index('poll_ssm() {'):shell.index('\n# JSON 인코딩')]
    output=tmp_path/'remote-output'
    output.write_text('secret-canary\nCHAT_CRYPTO_BENCHMARK '+json.dumps(_benchmark_metrics())+'\n')
    for name,source in [('sleep','exit 0'),('aws',
        'case "$*" in *StandardOutputContent*) cat "$REMOTE_OUTPUT" ;; *) echo Success ;; esac')]:
        path=tmp_path/name
        path.write_text('#!/bin/sh\n'+source+'\n')
        path.chmod(0o755)
    result=subprocess.run(['bash','-ec',function+'\npoll_ssm synthetic-command'],text=True,capture_output=True,
        env=dict(os.environ,PATH=str(tmp_path)+':'+os.environ['PATH'],TMPDIR=str(tmp_path),
                 AWS_REGION='ap-northeast-2',ID='synthetic',CRYPTO_BENCHMARK='true',REMOTE_OUTPUT=str(output)))
    assert result.returncode==0,result.stderr
    assert result.stderr=='' and 'secret-canary' not in result.stdout
    assert 'CHAT_CRYPTO_BENCHMARK ' in result.stdout and 'DEV_SSM_SUCCESS' in result.stdout
    assert not list(tmp_path.glob('tmp.*'))


def test_benchmark_pair_differences_allow_small_negative_noise_only(tmp_path):
    metrics=_benchmark_metrics()
    metrics['added_turn_median_ms']=-.1
    metrics['added_turn_p95_ms']=-.05
    metrics['added_window_read_median_ms']=-.2
    metrics['added_window_read_p95_ms']=.1
    path=tmp_path/'private-output'
    path.write_text('CHAT_CRYPTO_BENCHMARK '+json.dumps(metrics))
    result=subprocess.run([sys.executable,'-c',_benchmark_parser(),str(path)],text=True,capture_output=True)
    assert result.returncode==0 and result.stderr==''
    metrics['added_turn_median_ms']=-1001
    path.write_text('CHAT_CRYPTO_BENCHMARK '+json.dumps(metrics))
    blocked=subprocess.run([sys.executable,'-c',_benchmark_parser(),str(path)],text=True,capture_output=True)
    assert blocked.returncode!=0 and blocked.stdout==''


@pytest.mark.parametrize('preflight_success', [True,False])
def test_benchmark_dispatch_occurs_only_after_successful_preflight(tmp_path,preflight_success):
    command,_=_generated_dev_command(tmp_path,'true','true')
    diagnostic=command[command.index('umask 077'):]
    (tmp_path/'deploy.sh').write_text('PREFLIGHT_ONLY="${DEPLOY_PREFLIGHT_ONLY:-0}"\n')
    trace=tmp_path/'docker-trace'
    for name,source in [('sudo','printf "==> DB schema preflight\\n"; test "$PREFLIGHT_SUCCESS" = true'),
                        ('docker','echo called > "$TRACE"; echo "CHAT_CRYPTO_BENCHMARK {}"')]:
        path=tmp_path/name
        path.write_text('#!/bin/sh\n'+source+'\n')
        path.chmod(0o755)
    result=subprocess.run(['sh','-ec',diagnostic],cwd=tmp_path,text=True,capture_output=True,
        env=dict(os.environ,PATH=str(tmp_path)+':'+os.environ['PATH'],TMPDIR=str(tmp_path),
                 TRACE=str(trace),PREFLIGHT_SUCCESS='true' if preflight_success else 'false'))
    assert result.returncode==(0 if preflight_success else 185)
    assert trace.exists() is preflight_success
    assert result.stderr==''
    assert not list(tmp_path.glob('tmp.*'))
