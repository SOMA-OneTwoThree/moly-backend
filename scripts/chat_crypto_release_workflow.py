"""Trusted GHA-only metadata controller; no local/default AWS or user-data reads."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

ACCOUNT = '676972757138'
REGION = 'ap-northeast-2'
RESCUE_SHA = 'adc94a495d9c5bebe5d4fbe281fa81825c85beba'
SQL_SHA = 'b6b80ea155570de38c9ef4645ec3fc3874e17e67d0cc23604b90eb87112c1f8b'
TARGETS = {
    'production': ('main', 'moly-github-actions-role', 'moly-backend', 'qkgjlgzsharnilxnkytd', 2),
    'development': ('dev', 'moly-github-actions-dev-role', 'moly-backend-dev', 'wywzjslvxwttxkecbyis', 1),
}
TG = 'arn:aws:elasticloadbalancing:ap-northeast-2:676972757138:targetgroup/moly-backend-tg/5d725ff95a85e070'
PROOF_KEYS = {'capability','environment','version','started','format_version','active_key_id','key_fingerprints','schema','self_test'}

SAFE_REMOTE_REASONS = frozenset({'command_failed','private_proof_invalid','host_environment','infra_dirty',
    'infra_origin','infra_commit','role_environment_mismatch','role_environment_override','worker_command',
    'consumer_command','consumer_population','runtime_stopped','runtime_image','runtime_proof_changed',
    'health_failed','database_health_failed','worker_timer_failed','worker_launch_image','worker_proof_changed',
    'old_worker_running','unknown_action'})
SAFE_REMOTE_STAGES = frozenset({'host','infra','deploy','proof','role_equivalence','runtime','schema',
                               'daily_report','health','worker','activate'})


class Blocked(Exception):
    pass


class InvocationPending(Blocked):
    pass


def require_runner(environment):
    branch, *_ = TARGETS[environment]
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_REPOSITORY') != 'SOMA-OneTwoThree/moly-backend'
            or os.environ.get('GITHUB_REF') != 'refs/heads/' + branch
            or os.environ.get('GITHUB_EVENT_NAME') not in {'push','workflow_dispatch'}
            or not os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL')
            or not os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')):
        raise Blocked('approved_runner_required')


def proof(value, environment, sha):
    if (not isinstance(value,dict) or set(value) != PROOF_KEYS
            or value['capability'] != 'chat-content-aead-v1'
            or value['environment'] != environment or value['version'] != sha
            or type(value['started']) is not bool or type(value['format_version']) is not int or value['format_version'] != 1
            or type(value['active_key_id']) is not int or not 1 <= value['active_key_id'] <= 65535
            or value['schema'] != 'ok' or value['self_test'] != 'ok'
            or not isinstance(value['key_fingerprints'],dict) or str(value['active_key_id']) not in value['key_fingerprints']
            or any(not re.fullmatch(r'[1-9][0-9]{0,4}',k) or int(k)>65535
                   or not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{64}',v)
                   for k,v in value['key_fingerprints'].items())):
        raise Blocked('capability_proof_invalid')
    return value


# Fixed host program. Only validated hex/instance/environment metadata enters it.
REMOTE = r'''
import json,os,subprocess,sys,urllib.request
from pathlib import Path
c=CONFIGURATION
stage='host'
root=Path('/root/moly-infra')
state=root/'.deploy-state'
def call(argv, *, env=None, data=None, check=True, timeout=60):
    result=subprocess.run(argv,input=data,capture_output=True,text=True,env=env,timeout=timeout)
    if check and result.returncode: raise ValueError('command_failed')
    return result.stdout.strip()
def read(name):
    path=state/name
    if path.is_symlink() or path.stat().st_size>65536: raise ValueError('private_proof_invalid')
    return json.loads(path.read_text())
def same(a,b):
    return {k:v for k,v in a.items() if k!='started'}=={k:v for k,v in b.items() if k!='started'}
try:
    marker='prod' if c['environment']=='production' else 'dev'
    if Path('/etc/moly-env').read_text().strip()!=marker: raise ValueError('host_environment')
    os.chdir(root)
    action=c['action']
    if action in ('bootstrap','expand','preflight'):
        stage='infra'
        if call(['git','status','--porcelain']): raise ValueError('infra_dirty')
        if call(['git','remote','get-url','origin'])!='https://github.com/SOMA-OneTwoThree/moly-infra.git': raise ValueError('infra_origin')
        call(['git','fetch','origin',c['infra_sha']],timeout=120)
        if call(['git','rev-parse','FETCH_HEAD'])!=c['infra_sha']: raise ValueError('infra_commit')
        call(['git','checkout','--detach',c['infra_sha']])
        if call(['git','rev-parse','HEAD'])!=c['infra_sha']: raise ValueError('infra_commit')
        env=dict(os.environ)
        for key in ('DEPLOY_PREFLIGHT_ONLY','DEPLOY_CRYPTO_BOOTSTRAP','DEPLOY_CRYPTO_EXPAND'): env.pop(key,None)
        env['DEPLOY_PREFLIGHT_ONLY']='1'
        if action in ('bootstrap','expand'): env['DEPLOY_CRYPTO_BOOTSTRAP']='1'
        if action=='expand': env['DEPLOY_CRYPTO_EXPAND']='1'
        name='chat-content-bootstrap.json' if action in ('bootstrap','expand') else 'chat-content-proof.next.json'
        target=state/name
        if target.is_symlink(): raise ValueError('private_proof_invalid')
        target.unlink(missing_ok=True)
        stage='deploy'
        call(['bash','deploy.sh',c['tag']],env=env,timeout=1200)
        stage='proof'
        value=read(name)
        if action=='preflight':
            saved=state/('chat-content-preflight-'+c['sha']+'.json')
            if saved.is_symlink(): raise ValueError('private_proof_invalid')
            saved.write_text(json.dumps(value,sort_keys=True));saved.chmod(0o600)
            stage='role_equivalence'
            compose_env={**os.environ,'IMAGE_TAG':c['tag'],'IMAGE_REPO':c['image'],
                         'ECR_REGISTRY':'676972757138.dkr.ecr.ap-northeast-2.amazonaws.com'}
            compose=json.loads(call(['docker','compose','--env-file',str(root/'.env'),'-f',str(root/'docker-compose.yml'),
                                     '--profile','*','config','--no-env-resolution','--format','json'],env=compose_env))
            expected=compose_env['ECR_REGISTRY']+'/'+c['image']+':'+c['tag']
            for service in ('backend','consumer','worker'):
                role=compose['services'][service]
                files=[row['path'] if isinstance(row,dict) else row for row in role.get('env_file',[])]
                if role['image']!=expected or [str(Path(path).resolve()) for path in files]!=[str(root/'backend.env')]: raise ValueError('role_environment_mismatch')
                forbidden={'CHAT_CONTENT_ENCRYPTION_KEYS','ENVIRONMENT','GIT_SHA','SUPABASE_DB_CONNECTION_STRING'}
                if forbidden.intersection(role.get('environment',{})) or role.get('entrypoint') not in (None,[]): raise ValueError('role_environment_override')
                if service=='worker' and role.get('command')!=['python','-m','worker']: raise ValueError('worker_command')
                if service=='consumer' and role.get('command')!=['python','-m','worker.consumer']: raise ValueError('consumer_command')
            import hashlib
            basis={'kind':'immutable-image-common-environment-v1','image':expected,'roles':['api','consumer','worker'],
                   'environment_file':'backend.env','crypto_overrides':False,'source_infra_sha':c['infra_sha'],
                   'composition_hash':hashlib.sha256(json.dumps(compose,sort_keys=True).encode()).hexdigest()}
            value={'proof':value,'basis':basis}
        print(json.dumps(value,sort_keys=True))
    elif action=='collect':
        stage='runtime'
        consumers=call(['docker','ps','--filter','label=com.docker.compose.service=consumer','--format','{{.Names}}']).splitlines()
        if consumers!=['moly-consumer']: raise ValueError('consumer_population')
        value={}
        for role,name in (('api','moly-backend'),('consumer','moly-consumer')):
            if call(['docker','inspect','--format','{{.State.Running}}',name])!='true': raise ValueError('runtime_stopped')
            expected_image='676972757138.dkr.ecr.ap-northeast-2.amazonaws.com/'+c['image']+':'+c['tag']
            if call(['docker','inspect','--format','{{.Config.Image}}',name])!=expected_image: raise ValueError('runtime_image')
            actual=json.loads(call(['docker','exec',name,'python','scripts/check_chat_content_crypto.py',
                '--environment',c['environment'],'--expected-sha',c['sha']]))
            stored=read('chat-content-'+name+'.json')
            if not same(actual,stored): raise ValueError('runtime_proof_changed')
            value[role]=actual
        stage='schema'
        call(['docker','exec','moly-backend','python','-m','db.schema_contract'])
        stage='daily_report'
        call(['docker','exec','moly-backend','python','scripts/check_daily_report.py',
              '--environment',c['environment'],'--expected-sha',c['sha']],timeout=20)
        stage='health'
        for path in ('/health','/health/ready'):
            with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=8) as response:
                result=json.loads(response.read(16385))
                if response.status!=200 or result.get('version')!=c['sha'] or result.get('status')!='ok': raise ValueError('health_failed')
                if path.endswith('ready') and result.get('db')!='ok': raise ValueError('database_health_failed')
        stage='worker'
        timer=call(['systemctl','is-active','moly-worker.timer'],check=False)
        worker=Path('/etc/moly-worker-host').is_file()
        if timer!=('active' if worker else 'inactive'): raise ValueError('worker_timer_failed')
        expected_image='676972757138.dkr.ecr.ap-northeast-2.amazonaws.com/'+c['image']+':'+c['tag']
        compose=json.loads(call(['docker','compose','--env-file',str(root/'.env'),'-f',str(root/'docker-compose.yml'),'--profile','worker','config','--format','json']))
        if (compose['services']['worker']['image']!=expected_image
                or compose['services']['worker'].get('command')!=['python','-m','worker']): raise ValueError('worker_launch_image')
        worker_proof=json.loads(call(['docker','compose','--env-file',str(root/'.env'),'-f',str(root/'docker-compose.yml'),
            'run','--rm','--no-deps','-T','--entrypoint','python','worker','scripts/check_chat_content_crypto.py',
            '--environment',c['environment'],'--expected-sha',c['sha']]))
        if not same(worker_proof,read('chat-content-worker-launch.json')): raise ValueError('worker_proof_changed')
        for identity in call(['docker','ps','--filter','label=com.docker.compose.service=worker','--format','{{.ID}}']).splitlines():
            if call(['docker','inspect','--format','{{.Config.Image}}',identity])!=expected_image: raise ValueError('old_worker_running')
        value.update(worker_host=worker,timer=timer,worker_proof=worker_proof,
                     old_tick_drained=True,schema=True,daily_report=True,health=True)
        print(json.dumps(value,sort_keys=True))
    elif action=='activate':
        stage='activate'
        argv=['docker','exec','-i','moly-backend','python','scripts/activate_chat_content_crypto.py',
              '--environment',c['environment'],'--expected-project-ref',c['project_ref'],'--expected-sha',c['sha'],
              '--expected-rescue-sha',c['rescue_sha'],'--release-id',c['release_id'],'--worker-instance-id',c['worker_id']]
        for identity in c['instance_ids']: argv.extend(['--instance-id',identity])
        print(call(argv,data=json.dumps(c['receipt']),timeout=20))
    else: raise ValueError('unknown_action')
except BaseException as error:
    reason=error.args[0] if isinstance(error,ValueError) and error.args else None
    if type(reason) is not str or reason not in SAFE_REASONS_LITERAL: reason='command_failed'
    print(json.dumps({'status':'blocked','reason':reason,'action':c['action'],'stage':stage}))
    raise SystemExit(1) from None
'''.replace('SAFE_REASONS_LITERAL',repr(sorted(SAFE_REMOTE_REASONS)))


class Cloud:
    def __init__(self, environment):
        require_runner(environment)
        self.environment=environment
        actual=self.aws('sts','get-caller-identity')
        role=TARGETS[environment][1]
        if actual.get('Account')!=ACCOUNT or not str(actual.get('Arn','')).startswith(f'arn:aws:sts::{ACCOUNT}:assumed-role/{role}/'):
            raise Blocked('project_role_required')

    def aws(self,*args):
        result=subprocess.run(['aws',*args,'--region',REGION,'--output','json'],capture_output=True,text=True,timeout=40,
                              env={**os.environ,'AWS_PAGER':'','AWS_CLI_AUTO_PROMPT':'off'})
        if result.returncode:
            if args[:2]==('ssm','get-command-invocation') and 'InvocationDoesNotExist' in result.stderr:
                raise InvocationPending('invocation_pending')
            raise Blocked('aws_metadata_call_failed')
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise Blocked('aws_metadata_response_invalid') from None

    def fleet(self, identities):
        tag=TARGETS[self.environment][2]
        actual=self.aws('ec2','describe-instances','--filters',f'Name=tag:Role,Values={tag}',
            'Name=instance-state-name,Values=running','--query','Reservations[].Instances[].InstanceId')
        if not isinstance(actual,list) or len(actual)!=len(identities) or set(actual)!=set(identities):
            raise Blocked('exact_fleet_required')

    def healthy(self, identities):
        if self.environment!='production':
            return
        rows=self.aws('elbv2','describe-target-health','--target-group-arn',TG)['TargetHealthDescriptions']
        if len(rows)!=len(identities) or {r['Target']['Id'] for r in rows}!=set(identities) or any(r['TargetHealth']['State']!='healthy' for r in rows):
            raise Blocked('healthy_exact_fleet_required')

    def remote(self, identity, configuration):
        source=REMOTE.replace('CONFIGURATION',repr(configuration),1)
        command="python3 - <<'MOLY_CRYPTO_RELEASE'\n"+source+"\nMOLY_CRYPTO_RELEASE"
        # Metadata only, safely JSON-encoded; never place credentials in arguments.
        result=self.aws('ssm','send-command','--instance-ids',identity,'--document-name','AWS-RunShellScript',
            '--parameters',json.dumps({'commands':[command],'executionTimeout':['1200']}))
        command_id=result['Command']['CommandId']
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',command_id):
            raise Blocked('command_identity_invalid')
        deadline=time.monotonic()+1200
        attempts, failures=0,0
        while time.monotonic()<deadline:
            time.sleep(5)
            attempts+=1
            try:
                result=self.aws('ssm','get-command-invocation','--instance-id',identity,'--command-id',command_id)
            except InvocationPending:
                if attempts<=3:
                    continue
                raise Blocked('remote_status_unavailable') from None
            except Blocked:
                failures+=1
                if failures<3:
                    continue
                raise Blocked('remote_status_unavailable') from None
            failures=0
            status=result.get('Status')
            if status=='Success':
                raw=result.get('StandardOutputContent','')
                if not isinstance(raw,str) or len(raw)>65536:
                    raise Blocked('remote_metadata_invalid')
                return json.loads(raw)
            if status not in {'Pending','InProgress','Delayed'}:
                raw=result.get('StandardOutputContent','')
                try:
                    value=json.loads(raw) if isinstance(raw,str) and len(raw)<=65536 else None
                    if (isinstance(value,dict) and set(value)=={'status','reason','action','stage'}
                            and value['status']=='blocked' and value['reason'] in SAFE_REMOTE_REASONS
                            and value['stage'] in SAFE_REMOTE_STAGES and value['action']==configuration['action']):
                        raise Blocked('remote_'+value['action']+'_'+value['stage']+'_'+value['reason'])
                except (ValueError,TypeError):
                    pass
                raise Blocked('remote_step_failed')
        raise Blocked('remote_step_timeout')


def arguments(args):
    for value in (args.sha,args.rescue_sha,args.infra_sha):
        if not re.fullmatch(r'[0-9a-f]{40}',value) or value=='0'*40:
            raise Blocked('reviewed_commit_required')
    if args.sha==args.rescue_sha or not re.fullmatch(r'[0-9]{1,24}',args.release_id):
        raise Blocked('release_identity_invalid')
    if (len(args.instance_id)!=TARGETS[args.environment][4] or len(set(args.instance_id))!=len(args.instance_id)
            or any(not re.fullmatch(r'i-(?:[0-9a-f]{8}|[0-9a-f]{17})',value) for value in args.instance_id)):
        raise Blocked('exact_fleet_required')
    base=Path(os.environ.get('RUNNER_TEMP','')).resolve()
    path=Path(args.state)
    if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(base) or not os.environ.get('RUNNER_TEMP'):
        raise Blocked('private_state_path_required')
    return {'environment':args.environment,'sha':args.sha,'rescue_sha':args.rescue_sha,'infra_sha':args.infra_sha,
            'release_id':args.release_id,'instance_ids':sorted(args.instance_id),'project_ref':TARGETS[args.environment][3],
            'image':TARGETS[args.environment][2]}


def private_write(path,value):
    path=Path(path)
    if path.is_symlink():
        raise Blocked('private_state_path_required')
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    old=os.umask(0o077)
    try:
        with path.open('w') as file:
            json.dump(value,file,sort_keys=True,allow_nan=False)
        path.chmod(0o600)
    finally:
        os.umask(old)


def configuration(identity,action,sha=None):
    result={**identity,'action':action}
    if sha is not None:
        result['sha']=sha
    result['tag']=('dev-' if result['environment']=='development' else '')+result['sha']
    return result


def bootstrap(value, identity):
    if (not isinstance(value,dict) or value.get('format')!='chat-content-schema-v1'
            or value.get('environment')!=identity['environment'] or value.get('project_ref')!=identity['project_ref']
            or value.get('version')!=identity['sha'] or value.get('sql_sha256')!=SQL_SHA
            or value.get('self_test')!='ok' or value.get('schema_status') not in {'needs_expansion','expanded'}
            or not isinstance(value.get('key_fingerprints'),dict) or not isinstance(value.get('config_snapshot'),dict)):
        raise Blocked('bootstrap_receipt_invalid')
    return value


def preflight(value,identity,sha):
    expected_image=f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{identity['image']}:" + ('dev-' if identity['environment']=='development' else '') + sha
    if not isinstance(value,dict) or set(value)!={'proof','basis'}:
        raise Blocked('role_equivalence_required')
    basis=value['basis']
    if (not isinstance(basis,dict) or set(basis)!={'kind','image','roles','environment_file','crypto_overrides','source_infra_sha','composition_hash'}
            or basis['kind']!='immutable-image-common-environment-v1' or basis['image']!=expected_image
            or basis['roles']!=['api','consumer','worker'] or basis['environment_file']!='backend.env'
            or basis['crypto_overrides'] is not False or basis['source_infra_sha']!=identity['infra_sha']
            or not isinstance(basis['composition_hash'],str) or not re.fullmatch(r'[0-9a-f]{64}',basis['composition_hash'])):
        raise Blocked('role_equivalence_required')
    proof(value['proof'],identity['environment'],sha)
    return value


def prepare(args,cloud,identity):
    cloud.fleet(identity['instance_ids'])
    cloud.healthy(identity['instance_ids'])
    initial=[bootstrap(cloud.remote(host,configuration(identity,'bootstrap')),identity) for host in identity['instance_ids']]
    if any(row['key_fingerprints']!=initial[0]['key_fingerprints'] for row in initial):
        raise Blocked('fleet_key_mismatch')
    expanded=bootstrap(cloud.remote(identity['instance_ids'][0],configuration(identity,'expand')),identity)
    if expanded['schema_status']!='expanded':
        raise Blocked('schema_expansion_required')
    primary,rescue=[],[]
    for host in identity['instance_ids']:
        primary.append({'instance_id':host,**preflight(cloud.remote(host,configuration(identity,'preflight')),identity,identity['sha'])})
        rescue.append({'instance_id':host,**preflight(cloud.remote(host,configuration(identity,'preflight',identity['rescue_sha'])),identity,identity['rescue_sha'])})
    reference=primary[0]['proof']
    for item in [*primary,*rescue]:
        p=item['proof']
        if (p['key_fingerprints']!=expanded['key_fingerprints'] or p['active_key_id']!=reference['active_key_id']
                or p['format_version']!=reference['format_version']):
            raise Blocked('fleet_key_mismatch')
    private_write(args.state,{'identity':identity,'bootstrap':expanded,'primary':primary,'rescue':rescue})


def finalize(args,cloud,identity):
    path=Path(args.state)
    if path.stat().st_mode & 0o077 or path.stat().st_size>65536:
        raise Blocked('private_state_invalid')
    prepared=json.loads(path.read_text())
    if prepared.get('identity')!=identity:
        raise Blocked('prepared_identity_changed')
    cloud.fleet(identity['instance_ids'])
    cloud.healthy(identity['instance_ids'])
    rows=[]
    for host in identity['instance_ids']:
        row=cloud.remote(host,configuration(identity,'collect'))
        if any(row.get(gate) is not True for gate in ('schema','daily_report','health','old_tick_drained')):
            raise Blocked('actual_release_gate_failed')
        for role in ('api','consumer'):
            proof(row[role],identity['environment'],identity['sha'])
        proof(row['worker_proof'],identity['environment'],identity['sha'])
        rows.append({'instance_id':host,**row})
    workers=[row for row in rows if row.get('worker_host') is True and row.get('timer')=='active']
    if len(workers)!=1 or any(row.get('timer')!='inactive' for row in rows if row not in workers):
        raise Blocked('exact_worker_required')
    worker=workers[0]
    public_identity={'environment':identity['environment'],'project_ref':identity['project_ref'],
                     'release_sha':identity['sha'],'release_id':identity['release_id']}
    passed={**public_identity,'origin':'trusted-deploy-workflow','status':'passed'}
    rescue={item['instance_id']:preflight({'proof':item['proof'],'basis':item['basis']},identity,identity['rescue_sha'])['proof']
            for item in prepared['rescue']}
    if set(rescue)!=set(identity['instance_ids']) or len(prepared['rescue'])!=len(rescue):
        raise Blocked('exact_rescue_fleet_required')
    gates={name:dict(passed) for name in ('schema','daily_report','health')}
    gates['rollback_reader']={**passed,'rescue_sha':identity['rescue_sha'],
        'instances':[{'instance_id':host,'api':rescue[host],'consumer':rescue[host]} for host in identity['instance_ids']],
        'worker':{'instance_id':worker['instance_id'],'proof':rescue[worker['instance_id']]}}
    receipt={**public_identity,'format':'chat-content-release-v1',
        'instances':[{'instance_id':r['instance_id'],'api':r['api'],'consumer':r['consumer']} for r in rows],
        'worker':{'instance_id':worker['instance_id'],'proof':worker['worker_proof'],'old_tick_drained':True,'timer_restored':True},
        'gates':gates,'config_snapshot':prepared['bootstrap']['config_snapshot']}
    private_write(str(path)+'.receipt',receipt)
    result=cloud.remote(worker['instance_id'],{**configuration(identity,'activate'),'worker_id':worker['instance_id'],'receipt':receipt})
    if (result.get('format')!='chat-content-release-v1' or result.get('environment')!=identity['environment']
            or result.get('version')!=identity['sha'] or result.get('started') is not True
            or result.get('guard_enabled') is not True or result.get('release_id')!=identity['release_id']):
        raise Blocked('activation_not_confirmed')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('prepare','finalize'))
    parser.add_argument('--environment',choices=tuple(TARGETS),required=True)
    parser.add_argument('--sha',required=True)
    parser.add_argument('--rescue-sha',default=RESCUE_SHA)
    parser.add_argument('--infra-sha',required=True)
    parser.add_argument('--release-id',required=True)
    parser.add_argument('--instance-id',action='append',required=True)
    parser.add_argument('--state',required=True)
    try:
        args=parser.parse_args(argv)
        identity=arguments(args)
        cloud=Cloud(args.environment)
        (prepare if args.phase=='prepare' else finalize)(args,cloud,identity)
        print(json.dumps({'phase':args.phase,'status':'passed','environment':args.environment}))
        return 0
    except Exception as error:
        reason=error.args[0] if isinstance(error,Blocked) and error.args else None
        if type(reason) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,127}',reason):
            reason='chat_crypto_release_failed'
        print(json.dumps({'status':'blocked','reason':reason}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
