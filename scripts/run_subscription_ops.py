"""Fixed GitHub OIDC/SSM runner. Never prints command stdout/stderr or customer data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

ACCOUNT = "676972757138"
REGION = "ap-northeast-2"
ROLE = "moly-github-actions-role"
TG = "arn:aws:elasticloadbalancing:ap-northeast-2:676972757138:targetgroup/moly-backend-tg/5d725ff95a85e070"
ACTIONS = ("report", "repair_refunded", "repair_metadata", "verify", "activate", "deactivate")

REMOTE = r'''
import json,os,pathlib,re,subprocess
config=json.loads(CONFIG_LITERAL)
def public_summary(value):
    allowed={'ok','reason','timer','action','sha','manifest','membership','counts','new_adoption_enabled'}
    rejected={'ok':False,'reason':'admin_output_rejected_review_private_journal'}
    if not isinstance(value,dict) or set(value)-allowed or type(value.get('ok')) is not bool:return rejected
    for key in ('timer','new_adoption_enabled'):
        if key in value and type(value[key]) is not bool:return rejected
    for key in ('sha','manifest','membership'):
        if key in value and (not isinstance(value[key],str) or not re.fullmatch('[0-9a-f]{40}' if key=='sha' else '[0-9a-f]{64}',value[key])):return rejected
    if 'action' in value and value['action'] not in ('report','repair_refunded','repair_metadata','verify','activate','deactivate'):return rejected
    reasons={'runtime_target_mismatch','provider_read_settings_missing','invalid_private_audit_directory',
        'cohort_requires_manual_review','control_subscription_changed','private_journal_requires_review',
        'previous_commit_requires_review','cohort_repair_incomplete','repaired_subscription_changed',
        'refunded_access_remains','paid_access_not_preserved','related_inbox_changed','no_matching_cohort',
        'provider_ownership_changed','cohort_membership_or_counts_changed','manifest_mismatch',
        'refund_repair_must_finish_first','target_missing','completed_target_changed',
        'target_changed_since_manifest','fresh_plan_changed_from_manifest','committed_repair_requires_review',
        'activation_configuration_changed','configuration_commit_unknown','configuration_committed_audit_failed',
        'invalid_operation_arguments','operation_failed_review_private_journal',
        'admin_result_unavailable_review_private_journal'}
    if 'reason' in value and value['reason'] not in reasons:return rejected
    if 'counts' in value and (not isinstance(value['counts'],dict) or set(value['counts'])!={'refunded','metadata','controls'} or any(type(n) is not int or not 0<=n<=100000 for n in value['counts'].values())):return rejected
    return value
def call(argv,timeout=30):
    result=subprocess.run(argv,capture_output=True,text=True,timeout=timeout,check=False)
    if result.returncode: raise RuntimeError('remote_command_failed')
    return result.stdout.strip()
try:
    expected=config['sha']
    image='676972757138.dkr.ecr.ap-northeast-2.amazonaws.com/moly-backend:'+expected
    code='from app.services.subscription_ops import require_runtime;require_runtime('+repr(expected)+',require_provider='+repr(config['action']!='deactivate')+')'
    for name in (() if config['action']=='deactivate' else ('moly-backend','moly-consumer')):
        assert call(['docker','inspect','--format','{{.State.Running}}',name])=='true'
        call(['docker','exec',name,'python','-B','-c',code],timeout=30)
    if config['action']!='deactivate':
        compose=json.loads(call(['docker','compose','--profile','*','--env-file','/root/moly-infra/.env',
            '-f','/root/moly-infra/docker-compose.yml','config','--format','json']))
        assert compose['services']['worker']['image']==image
        running=call(['docker','ps','--filter','label=com.docker.compose.service=worker','--format','{{.ID}}'])
        for identity in running.splitlines():
            call(['docker','exec',identity,'python','-B','-c',code],timeout=30)
    timer=subprocess.run(['systemctl','is-active','moly-worker.timer'],capture_output=True,text=True,check=False)
    active=timer.stdout.strip()=='active'
    if active and config['action']!='deactivate':
        unit=call(['systemctl','show','moly-worker.service','--property=ExecStart','--value'])
        assert '/root/moly-infra/' in unit and 'worker' in unit
        assert pathlib.Path('/etc/moly-worker-host').is_file()
    if config['mode']=='check':
        print(json.dumps({'ok':True,'timer':active}))
    else:
        audit=pathlib.Path('/var/lib/moly/subscription-ops')
        for parent in (audit,*audit.parents):
            assert not parent.is_symlink() and not (parent/'.git').exists()
        audit.mkdir(mode=0o700,parents=True,exist_ok=True)
        uid=int(call(['docker','run','--rm','--entrypoint','id',image,'-u']))
        gid=int(call(['docker','run','--rm','--entrypoint','id',image,'-g']))
        os.chown(audit,uid,gid);os.chmod(audit,0o700)
        argv=['docker','run','--rm','--read-only','--network','host','--cap-drop','ALL',
            '--security-opt','no-new-privileges','--tmpfs','/tmp:rw,noexec,nosuid,size=32m',
            '--env-file','/root/moly-infra/backend.env','-e','PYTHONDONTWRITEBYTECODE=1',
            '-e','PYTHONWARNINGS=ignore','--mount','type=bind,src='+str(audit)+',dst=/audit',
            image,'python','-B','-m','app.services.subscription_ops',config['action'],
            '--expected-sha',expected,'--session',config['session'],
            '--expected-manifest',config['manifest'],'--expected-membership',config['membership'],
            '--expected-counts',config['counts']]
        result=subprocess.run(argv,capture_output=True,text=True,timeout=500,check=False)
        try: summary=json.loads(result.stdout.splitlines()[-1])
        except BaseException: summary={'ok':False,'reason':'admin_result_unavailable_review_private_journal'}
        print(json.dumps(public_summary(summary)))
except BaseException:
    print(json.dumps({'ok':False,'reason':'runtime_or_admin_precheck_failed'}))
'''


class Stopped(Exception):
    pass


def require_runner():
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_REPOSITORY") != "SOMA-OneTwoThree/moly-backend"
            or os.environ.get("AWS_PROFILE") or not os.environ.get("AWS_ACCESS_KEY_ID")
            or not os.environ.get("AWS_SESSION_TOKEN")):
        raise Stopped("confirmed_project_oidc_runner_required")


def aws(*args) -> dict:
    try:
        result = subprocess.run(["aws", *args, "--region", REGION, "--output", "json"],
                                capture_output=True, text=True, timeout=30, check=False)
    except (subprocess.TimeoutExpired, OSError):
        raise Stopped("project_api_call_failed") from None
    if result.returncode:
        raise Stopped("project_api_call_failed")
    return json.loads(result.stdout)


def ssm(identity: str, config: dict) -> dict:
    source = REMOTE.replace("CONFIG_LITERAL", repr(json.dumps(config)))
    command = "python3 - <<'PY'\n" + source + "\nPY"
    with tempfile.TemporaryDirectory() as folder:
        parameters = Path(folder) / "parameters.json"
        parameters.write_text(json.dumps({"commands": [command], "executionTimeout": ["600"]}))
        os.chmod(parameters, 0o600)
        response = aws("ssm", "send-command", "--instance-ids", identity, "--document-name", "AWS-RunShellScript",
                       "--parameters", "file://" + str(parameters))
    command_id = response["Command"]["CommandId"]
    for _ in range(120):
        time.sleep(5)
        try:
            result = aws("ssm", "get-command-invocation", "--command-id", command_id, "--instance-id", identity)
        except Stopped:
            continue
        if result["Status"] in {"Pending", "InProgress", "Delayed"}:
            continue
        if result["Status"] != "Success":
            raise Stopped("remote_operation_failed_review_private_journal")
        try:
            summary = json.loads(result.get("StandardOutputContent", "").splitlines()[-1])
        except (ValueError, IndexError):
            raise Stopped("remote_result_unavailable_review_private_journal") from None
        return safe_summary(summary)
    raise Stopped("remote_operation_timeout_review_private_journal")


def safe_summary(value: dict) -> dict:
    allowed = {"ok", "reason", "timer", "action", "sha", "manifest", "membership", "counts", "new_adoption_enabled"}
    if not isinstance(value, dict) or set(value) - allowed or type(value.get("ok")) is not bool:
        raise Stopped("unrecognized_remote_result")
    for key in ("timer", "new_adoption_enabled"):
        if key in value and type(value[key]) is not bool:
            raise Stopped("unrecognized_remote_result")
    for key in ("manifest", "membership", "sha"):
        if key in value and not re.fullmatch(r"[0-9a-f]{40}" if key == "sha" else r"[0-9a-f]{64}", value[key]):
            raise Stopped("unrecognized_remote_result")
    if "reason" in value and not re.fullmatch(r"[a-z_]{1,100}", value["reason"]):
        raise Stopped("unrecognized_remote_result")
    if "action" in value and value["action"] not in ACTIONS:
        raise Stopped("unrecognized_remote_result")
    if "counts" in value and (not isinstance(value["counts"], dict) or set(value["counts"]) != {"refunded", "metadata", "controls"}
                              or any(type(v) is not int or not 0 <= v <= 100000 for v in value["counts"].values())):
        raise Stopped("unrecognized_remote_result")
    return value


def run(args):
    require_runner()  # Must precede even a caller-identity request; never use local/default AWS.
    if (not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha)
            or not re.fullmatch(r"[a-z0-9-]{8,80}", args.session)
            or (args.action != "deactivate" and not re.fullmatch(r"[0-9a-f]{64}", args.expected_membership))
            or (args.action != "deactivate" and not re.fullmatch(r"\d{1,5},\d{1,5},\d{1,5}", args.expected_counts))
            or (args.action not in {"report", "deactivate"} and not re.fullmatch(r"[0-9a-f]{64}", args.expected_manifest))):
        raise Stopped("invalid_operation_arguments")
    identity = aws("sts", "get-caller-identity")
    if identity.get("Account") != ACCOUNT or not identity.get("Arn", "").startswith(f"arn:aws:sts::{ACCOUNT}:assumed-role/{ROLE}/"):
        raise Stopped("project_oidc_identity_mismatch")
    response = aws("ec2", "describe-instances", "--filters", "Name=tag:Role,Values=moly-backend",
                   "Name=instance-state-name,Values=running")
    instances = sorted(i["InstanceId"] for r in response["Reservations"] for i in r["Instances"])
    targets = aws("elbv2", "describe-target-health", "--target-group-arn", TG)["TargetHealthDescriptions"] if args.action != "deactivate" else []
    if not instances or (args.action != "deactivate" and (len(instances) != 2 or len(targets) != 2
            or any(t["TargetHealth"]["State"] != "healthy" for t in targets)
            or {t["Target"]["Id"] for t in targets} != set(instances))):
        raise Stopped("production_fleet_not_healthy")
    config = {"sha": args.expected_sha, "action": args.action, "session": args.session,
              "manifest": args.expected_manifest, "membership": args.expected_membership,
              "counts": args.expected_counts, "mode": "check"}
    if args.action != "deactivate":
        checks = [ssm(host, config) for host in instances]
        if any(result.get("ok") is not True for result in checks) or sum(result.get("timer") is True for result in checks) != 1:
            raise Stopped("production_runtime_or_worker_ownership_mismatch")
    # A stable host is used across actions. Replacement/changed membership causes
    # a missing manifest and stops; never reconstruct a partially applied session.
    config["mode"] = "execute"
    if args.action == "deactivate":
        result = {"ok": False, "reason": "remote_operation_failed_review_private_journal"}
        for host in instances:
            try:
                result = ssm(host, config)
            except Stopped:
                continue
            if result.get("ok") is True:
                break
    else:
        result = ssm(instances[0], config)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") is True else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--expected-manifest", default="")
    parser.add_argument("--expected-membership", default="")
    parser.add_argument("--expected-counts", default="")
    try:
        return run(parser.parse_args())
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, Stopped) else "operation_driver_failed_review_private_journal"
        print(json.dumps({"ok": False, "reason": reason}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
