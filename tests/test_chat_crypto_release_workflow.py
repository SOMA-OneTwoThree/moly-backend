"""Workflow/source and fake-cloud checks. No AWS, host, DB or provider access."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from scripts import chat_crypto_release_workflow as w
from scripts import activate_chat_content_crypto as controller

SHA='a'*40
INFRA='b'*40
IDS=['i-11111111','i-22222222']
ROOT=Path(__file__).resolve().parents[1]


def runtime(environment,sha=SHA):
    return {'capability':'chat-content-aead-v1','environment':environment,'version':sha,
            'started':False,'format_version':1,'active_key_id':1,'key_fingerprints':{'1':'c'*64},
            'schema':'ok','self_test':'ok'}


def report_metadata(environment,sha=SHA):
    return {'daily_report':'ok','environment':environment,'version':sha,
            'read_only':True,'slack_sent':False,'elapsed_ms':1}


def setup_args(tmp_path,monkeypatch,environment='production'):
    monkeypatch.setenv('RUNNER_TEMP',str(tmp_path))
    return SimpleNamespace(environment=environment,sha=SHA,rescue_sha=w.RESCUE_SHA,infra_sha=INFRA,
        release_id='1234',instance_id=IDS if environment=='production' else IDS[:1],state=str(tmp_path/'private'/'state.json'))


class FakeCloud:
    def __init__(self,environment,fail=None):
        self.environment,self.fail,self.calls,self.receipt=environment,fail,[],None

    def fleet(self,ids):
        self.calls.append(('fleet',tuple(ids)))

    def healthy(self,ids):
        self.calls.append(('healthy',tuple(ids)))

    def remote(self,host,c):
        action=c['action']
        self.calls.append((action,host,c['sha']))
        if self.fail==action:
            raise w.Blocked('fake_failure')
        current=runtime(c['environment'],c['sha'])
        if action in {'bootstrap','expand'}:
            mode={'started':False,'format_version':1,'active_key_id':1,'required_key_ids':[1]}
            snapshot={key:{'exists':key==controller.STATE_KEY,'value':mode if key==controller.STATE_KEY else None}
                      for key in controller.CONFIG_KEYS}
            return {'format':'chat-content-schema-v1','environment':c['environment'],'project_ref':c['project_ref'],
                    'version':c['sha'],'key_fingerprints':current['key_fingerprints'],'self_test':'ok',
                    'sql_sha256':w.SQL_SHA,'schema_status':'expanded' if action=='expand' else 'needs_expansion',
                    'schema_fingerprint':'d'*64,'applied':action=='expand','config_snapshot':snapshot}
        if action=='preflight':
            if self.fail=='rescue' and c['sha']==w.RESCUE_SHA:
                raise w.Blocked('rescue_failed')
            basis={'kind':'immutable-image-common-environment-v1',
                   'image':f"{w.ACCOUNT}.dkr.ecr.{w.REGION}.amazonaws.com/{c['image']}:{c['tag']}",
                   'roles':['api','consumer','worker'],'environment_file':'backend.env','crypto_overrides':False,
                   'source_infra_sha':c['infra_sha'],'composition_hash':'d'*64}
            if self.fail=='basis':
                basis['crypto_overrides']=True
            return {'proof':current,'basis':basis,'daily_report':report_metadata(c['environment'],c['sha'])}
        if action=='collect':
            worker=host==IDS[0]
            result={'api':current,'consumer':dict(current),'worker_proof':dict(current),
                    'worker_host':worker,'timer':'active' if worker else 'inactive',
                    'old_tick_drained':True,'schema':True,'daily_report':True,'health':True}
            if self.fail in {'schema','daily_report','health','old_tick_drained'}:
                result[self.fail]=False
            if self.fail=='timer':
                result['timer']='active'
            if self.fail=='proof':
                result['consumer']={**current,'version':'f'*40}
            return result
        if action=='activate':
            self.receipt=c['receipt']
            controller.validate_release(self.receipt,environment=c['environment'],project_ref=c['project_ref'],
                release_sha=c['sha'],release_id=c['release_id'],instance_ids=c['instance_ids'],
                worker_instance_id=c['worker_id'],current=current,expected_rescue_sha=c['rescue_sha'])
            return {'format':'chat-content-release-v1','environment':c['environment'],'version':c['sha'],
                    'started':True,'guard_enabled':True,'release_id':c['release_id']}
        raise AssertionError(action)


@pytest.mark.parametrize('environment',['production','development'])
def test_same_controller_prepares_and_atomically_activates_exact_environment(tmp_path,monkeypatch,environment):
    args=setup_args(tmp_path,monkeypatch,environment)
    identity=w.arguments(args)
    cloud=FakeCloud(environment)
    w.prepare(args,cloud,identity)
    calls=[r[0] for r in cloud.calls]
    count=len(args.instance_id)
    assert calls[2:2+count]==['bootstrap']*count
    assert calls[2+count]=='expand'
    assert calls[3+count:]==['preflight']*(2*count)
    assert Path(args.state).stat().st_mode & 0o077==0
    prepared=json.loads(Path(args.state).read_text())
    assert {r['proof']['version'] for r in prepared['primary']}=={SHA}
    assert {r['proof']['version'] for r in prepared['rescue']}=={w.RESCUE_SHA}
    w.finalize(args,cloud,identity)
    assert len([r for r in cloud.calls if r[0]=='activate'])==1
    assert cloud.receipt['worker']['old_tick_drained'] is True
    assert cloud.receipt['worker']['timer_restored'] is True
    assert Path(args.state+'.receipt').stat().st_mode & 0o077==0


@pytest.mark.parametrize('failure',['bootstrap','expand','rescue','basis'])
def test_prepare_failure_never_collects_or_activates(tmp_path,monkeypatch,failure):
    args=setup_args(tmp_path,monkeypatch)
    cloud=FakeCloud(args.environment,failure)
    with pytest.raises(w.Blocked):
        w.prepare(args,cloud,w.arguments(args))
    assert not Path(args.state).exists()
    assert all(row[0] not in {'collect','activate'} for row in cloud.calls)


@pytest.mark.parametrize('failure',['schema','daily_report','health','old_tick_drained','timer','proof'])
def test_real_gate_failure_cannot_be_converted_into_passed_receipt(tmp_path,monkeypatch,failure):
    args=setup_args(tmp_path,monkeypatch)
    cloud=FakeCloud(args.environment)
    identity=w.arguments(args)
    w.prepare(args,cloud,identity)
    cloud.fail=failure
    with pytest.raises(w.Blocked):
        w.finalize(args,cloud,identity)
    assert cloud.receipt is None


def test_mutated_release_identity_or_public_artifact_is_rejected(tmp_path,monkeypatch):
    args=setup_args(tmp_path,monkeypatch)
    cloud=FakeCloud(args.environment)
    identity=w.arguments(args)
    w.prepare(args,cloud,identity)
    with pytest.raises(w.Blocked):
        w.finalize(args,cloud,{**identity,'sha':'e'*40})
    Path(args.state).chmod(0o644)
    with pytest.raises(w.Blocked):
        w.finalize(args,cloud,identity)


@pytest.mark.parametrize('environment',['production','development'])
def test_wrong_fleet_or_equal_rescue_is_rejected_before_cloud(tmp_path,monkeypatch,environment):
    args=setup_args(tmp_path,monkeypatch,environment)
    args.instance_id=IDS[:1] if environment=='production' else IDS
    with pytest.raises(w.Blocked):
        w.arguments(args)
    args.instance_id=IDS if environment=='production' else IDS[:1]
    args.rescue_sha=SHA
    with pytest.raises(w.Blocked):
        w.arguments(args)


def test_local_default_aws_is_never_called(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    execute=Mock(side_effect=AssertionError('must not execute AWS locally'))
    monkeypatch.setattr(w.subprocess,'run',execute)
    with pytest.raises(w.Blocked):
        w.Cloud('production')
    execute.assert_not_called()


def test_development_role_cannot_accept_production_identity(monkeypatch):
    for key,value in {'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'SOMA-OneTwoThree/moly-backend',
        'GITHUB_REF':'refs/heads/dev','GITHUB_EVENT_NAME':'workflow_dispatch',
        'ACTIONS_ID_TOKEN_REQUEST_URL':'synthetic','ACTIONS_ID_TOKEN_REQUEST_TOKEN':'synthetic'}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.setattr(w.Cloud,'aws',lambda *a: {'Account':w.ACCOUNT,
        'Arn':f'arn:aws:sts::{w.ACCOUNT}:assumed-role/moly-github-actions-role/synthetic'})
    with pytest.raises(w.Blocked):
        w.Cloud('development')


@pytest.mark.parametrize('field,value',[('started',1),('version','f'*40),('format_version',True),
                                       ('key_fingerprints',{'1':'private-canary'})])
def test_proof_rejects_wrong_types_or_code(field,value):
    with pytest.raises(w.Blocked):
        w.proof({**runtime('production'),field:value},'production',SHA)


def test_fixed_host_program_compiles_and_never_prints_command_errors():
    source=w.REMOTE.replace('CONFIGURATION',repr({'action':'collect','sha':SHA}),1)
    compile(source,'fixed-host','exec')
    assert 'capture_output=True' in source
    assert "read('chat-content-worker-launch.json')" in source
    assert "timeout=1200" in source
    assert 'docker logs' not in source
    assert 'print(result.stderr' not in source
    assert "'DEPLOY_CRYPTO_EXPAND'" in source
    assert "'chat-content-preflight-'+c['sha']" in source


def test_workflow_orders_real_preparation_rolling_health_and_final_activation():
    source=(ROOT/'.github/workflows/deploy.yml').read_text()
    workflow=yaml.safe_load(source)
    steps=workflow['jobs']['build-and-deploy']['steps']
    names=[step['name'] for step in steps]
    assert names.index('Validate production release inputs before credentials')<names.index('Configure AWS credentials (OIDC)')
    assert names.index('Build and push image')<names.index('Checkout immutable crypto-aware rescue source')
    rescue=next(step for step in steps if step['name']=='Build and push immutable crypto-aware rescue')
    assert rescue['with']['context']=='.crypto-rescue-source'
    assert rescue['with']['build-args']=='GIT_SHA=${{ env.CRYPTO_RESCUE_SHA }}\n'
    assert 'latest' not in rescue['with']['tags']
    assert names.index('Post-deploy health smoke')<names.index('Atomically activate verified encryption and guard')
    rolling=next(step['run'] for step in steps if step['name']=='Rolling deploy via SSM')
    assert rolling.index('chat_crypto_release_workflow.py prepare')<rolling.index('aws elbv2 deregister-targets')
    assert 'git pull --ff-only' not in rolling
    assert 'git checkout --detach ${CRYPTO_INFRA_SHA}' in rolling
    assert '--query "StandardErrorContent" --output text || true' not in rolling
    assert 'secret' not in json.dumps(w.TARGETS)


def test_ssm_registration_grace_and_fixed_error_never_use_raw_output(monkeypatch):
    cloud=object.__new__(w.Cloud)
    responses=[{'Command':{'CommandId':'11111111-1111-1111-1111-111111111111'}},
               w.InvocationPending('pending'),w.InvocationPending('pending'),
               {'Status':'Success','StandardOutputContent':'{"metadata":true}'}]
    def aws(*args):
        result=responses.pop(0)
        if isinstance(result,Exception):
            raise result
        return result
    monkeypatch.setattr(cloud,'aws',aws)
    monkeypatch.setattr(w.time,'sleep',lambda _:None)
    assert cloud.remote(IDS[0],{'action':'collect'})=={'metadata':True}
    assert not responses


def test_current_single_key_metadata_is_below_ssm_output_limit():
    cloud=FakeCloud('production')
    args=SimpleNamespace(environment='production',sha=SHA,rescue_sha=w.RESCUE_SHA,infra_sha=INFRA,
                         release_id='1234',instance_ids=IDS,project_ref=w.TARGETS['production'][3],image='moly-backend')
    result=cloud.remote(IDS[0],w.configuration(vars(args),'collect'))
    assert len(json.dumps(result).encode())<4096


def _profiled_services(argv,services):
    # Compose excludes optional-profile services unless explicitly activated.
    profiles={argv[index+1] for index,value in enumerate(argv[:-1]) if value=='--profile'}
    return {name:value for name,value in services.items()
            if name=='backend' or '*' in profiles or name in profiles}


@pytest.mark.parametrize('mutation',[None,'absent_production_marker','environment_override','wrong_image','missing_profile'])
def test_remote_preflight_executes_fresh_check_and_proves_common_role_environment(tmp_path,monkeypatch,capsys,mutation):
    import subprocess
    root=tmp_path/'infra'
    state=root/'.deploy-state'
    state.mkdir(parents=True)
    marker=tmp_path/'environment'
    if mutation!='absent_production_marker':
        marker.write_text('prod')
    target=state/'chat-content-proof.next.json'
    target.write_text(json.dumps({'stale':True}))
    config={'action':'preflight','environment':'production','sha':SHA,'tag':SHA,'infra_sha':INFRA,'image':'moly-backend'}
    expected=f'{w.ACCOUNT}.dkr.ecr.{w.REGION}.amazonaws.com/moly-backend:{SHA}'
    services={role:{'image':expected,'env_file':[{'path':str(root/'backend.env'),'required':True}]}
              for role in ('backend','consumer','worker')}
    services['worker']['command']=['python','-m','worker']
    services['consumer']['command']=['python','-m','worker.consumer']
    if mutation=='environment_override':
        services['consumer']['environment']={'CHAT_CONTENT_ENCRYPTION_KEYS':'secret-canary'}
    elif mutation=='wrong_image':
        services['worker']['image']='unrelated'
    calls=[]
    def execute(argv,**kw):
        calls.append(argv)
        assert kw['capture_output'] is True and kw['text'] is True
        output=''
        if argv[:3]==['git','remote','get-url']:
            output='https://github.com/SOMA-OneTwoThree/moly-infra.git'
        elif argv[:2]==['git','rev-parse']:
            output=INFRA
        elif argv[:2]==['bash','deploy.sh']:
            assert not target.exists(), 'previous proof must be removed before checking'
            assert kw['env']['DEPLOY_PREFLIGHT_ONLY']=='1'
            assert 'DEPLOY_CRYPTO_BOOTSTRAP' not in kw['env']
            target.write_text(json.dumps(runtime('production')))
            (state/'daily-report-candidate.json').write_text(json.dumps(report_metadata('production')))
        elif argv[:2]==['docker','compose']:
            assert '--no-env-resolution' in argv
            assert kw['env']['IMAGE_TAG']==SHA
            output=json.dumps({'services':_profiled_services(argv,services)})
        elif argv[0]!='git':
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0,stdout=output,stderr='private-command-canary')
    monkeypatch.setattr(subprocess,'run',execute)
    monkeypatch.chdir(tmp_path)
    source=w.REMOTE.replace('CONFIGURATION',repr(config),1).replace("Path('/root/moly-infra')",f'Path({str(root)!r})').replace("Path('/etc/moly-env')",f'Path({str(marker)!r})')
    if mutation=='missing_profile':
        source=source.replace("'--profile','*',",'')
    if mutation in {'environment_override','wrong_image','missing_profile'}:
        with pytest.raises(SystemExit):
            exec(compile(source,'remote-preflight','exec'),{})
        blocked=json.loads(capsys.readouterr().out)
        assert blocked['status']=='blocked' and blocked['stage']=='role_equivalence'
        assert blocked['reason'] in w.SAFE_REMOTE_REASONS
    else:
        exec(compile(source,'remote-preflight','exec'),{})
        result=json.loads(capsys.readouterr().out)
        assert result['proof']==runtime('production')
        assert result['basis']['kind']=='immutable-image-common-environment-v1'
        assert result['basis']['roles']==['api','consumer','worker']
        assert (state/f'chat-content-preflight-{SHA}.json').exists()
    assert sum(argv[:2]==['bash','deploy.sh'] for argv in calls)==1


@pytest.mark.parametrize('missing_profile',[False,True])
def test_remote_collect_requires_worker_profile_before_fresh_worker_check(tmp_path,monkeypatch,capsys,missing_profile):
    import subprocess
    import urllib.request
    root=tmp_path/'infra'
    state=root/'.deploy-state'
    state.mkdir(parents=True)
    marker=tmp_path/'environment'
    marker.write_text('prod')
    worker_marker=tmp_path/'worker-host'
    worker_marker.touch()
    current=runtime('production')
    for name in ('moly-backend','moly-consumer','worker-launch'):
        (state/f'chat-content-{name}.json').write_text(json.dumps(current))
    expected=f'{w.ACCOUNT}.dkr.ecr.{w.REGION}.amazonaws.com/moly-backend:{SHA}'
    config={'action':'collect','environment':'production','sha':SHA,'tag':SHA,'infra_sha':INFRA,'image':'moly-backend'}
    worker_runs=[]
    services={'backend':{'image':expected},'worker':{'image':expected,'command':['python','-m','worker']}}
    def execute(argv,**kwargs):
        assert kwargs['capture_output'] is True
        output=''
        if argv[:2]==['docker','ps']:
            output='moly-consumer' if 'label=com.docker.compose.service=consumer' in argv else ''
        elif argv[:2]==['docker','inspect']:
            output='true' if '{{.State.Running}}' in argv else expected
        elif argv[:2]==['docker','exec']:
            output=(json.dumps(current) if 'scripts/check_chat_content_crypto.py' in argv else
                    json.dumps(report_metadata('production')) if 'scripts/check_daily_report.py' in argv else '')
        elif argv[:2]==['docker','compose']:
            if 'config' in argv:
                output=json.dumps({'services':_profiled_services(argv,services)})
            else:
                assert 'run' in argv and 'scripts/check_chat_content_crypto.py' in argv
                worker_runs.append(argv)
                output=json.dumps(current)
        elif argv[:2]==['systemctl','is-active']:
            output='active'
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0,stdout=output,stderr='private-canary')
    class Response:
        status=200
        def __enter__(self):
            return self
        def __exit__(self,*args):
            return False
        def read(self,size):
            return json.dumps({'status':'ok','db':'ok','version':SHA}).encode()
    monkeypatch.setattr(subprocess,'run',execute)
    monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**kw:Response())
    monkeypatch.chdir(tmp_path)
    source=w.REMOTE.replace('CONFIGURATION',repr(config),1).replace("Path('/root/moly-infra')",f'Path({str(root)!r})').replace("Path('/etc/moly-env')",f'Path({str(marker)!r})').replace("Path('/etc/moly-worker-host')",f'Path({str(worker_marker)!r})')
    if missing_profile:
        source=source.replace("'--profile','worker',",'')
        with pytest.raises(SystemExit):
            exec(compile(source,'remote-collect','exec'),{})
        assert json.loads(capsys.readouterr().out)['status']=='blocked'
        assert not worker_runs
    else:
        exec(compile(source,'remote-collect','exec'),{})
        result=json.loads(capsys.readouterr().out)
        assert result['worker_proof']==current and result['worker_host'] is True
        assert len(worker_runs)==1


@pytest.mark.parametrize('remote_reason,expected',[
    ({'status':'blocked','action':'collect','stage':'worker','reason':'worker_launch_image'},
     'remote_collect_worker_worker_launch_image'),
    ({'status':'blocked','action':'collect','stage':'worker','reason':'private-secret-canary'},'remote_step_failed'),
    ({'status':'blocked','action':'activate','stage':'worker','reason':'worker_launch_image'},'remote_step_failed'),
])
def test_failed_ssm_propagates_only_fixed_action_stage_and_reason(monkeypatch,remote_reason,expected):
    cloud=object.__new__(w.Cloud)
    responses=iter([{'Command':{'CommandId':'11111111-1111-1111-1111-111111111111'}},
                    {'Status':'Failed','StandardOutputContent':json.dumps(remote_reason),'StandardErrorContent':'private-secret-canary'}])
    monkeypatch.setattr(cloud,'aws',lambda *args:next(responses))
    monkeypatch.setattr(w.time,'sleep',lambda _:None)
    with pytest.raises(w.Blocked) as error:
        cloud.remote(IDS[0],{'action':'collect'})
    assert str(error.value)==expected and 'canary' not in str(error.value)


@pytest.mark.parametrize('environment,marker,accepted',[
    ('production',None,True),('production',b'prod\n',True),
    ('development',b'dev\n',True),('development',None,False),
    ('production',b'dev',False),('development',b'prod',False),
    ('production',b'',False),('production',b' prod\n',False),
    ('production',b'prod\r\n',False),('development',b'dev \n',False),
])
def test_remote_host_gate_matches_shell_legacy_default_without_cross_environment_fallback(
        tmp_path,monkeypatch,capsys,environment,marker,accepted):
    root=tmp_path/'infra'
    root.mkdir()
    path=tmp_path/'moly-env'
    if marker is not None:
        path.write_bytes(marker)
    config={'action':'invalid','environment':environment}
    source=w.REMOTE.replace('CONFIGURATION',repr(config),1).replace(
        "Path('/root/moly-infra')",f'Path({str(root)!r})').replace(
        "Path('/etc/moly-env')",f'Path({str(path)!r})')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        exec(compile(source,'remote-host-gate','exec'),{})
    value=json.loads(capsys.readouterr().out)
    # An invalid action is reached only if the host gate accepted the target.
    assert value['reason']==('unknown_action' if accepted else 'host_environment')


def test_unreadable_host_marker_is_not_treated_as_legacy_production(tmp_path,monkeypatch,capsys):
    path=tmp_path/'moly-env'
    def unreadable(self):
        raise PermissionError('private-marker-canary')
    monkeypatch.setattr(Path,'read_bytes',unreadable)
    source=w.REMOTE.replace('CONFIGURATION',repr({'action':'invalid','environment':'production'}),1).replace(
        "Path('/etc/moly-env')",f'Path({str(path)!r})')
    with pytest.raises(SystemExit):
        exec(compile(source,'remote-host-gate','exec'),{})
    raw=capsys.readouterr().out
    assert json.loads(raw)['reason']=='command_failed'
    assert 'canary' not in raw
