"""Trusted development deployment controls. Never print subprocess or HTTP bodies."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import HTTPRedirectHandler, build_opener

SHA = re.compile(r"[0-9a-f]{40}\Z")
DEV_URL = "https://dev.moly.asia"
REGION = "ap-northeast-2"
REPOSITORY = "moly-backend-dev"
DEV_ROLE = "arn:aws:iam::676972757138:role/moly-github-actions-dev-role"
FLAGS = ("context_checkpoint_enabled", "current_turn_context_enabled")


class GuardError(Exception):
    """Contains a fixed diagnostic code, never a remote response."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GuardError(code)


def valid_sha(value: object) -> bool:
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def run(args: list[str], *, timeout: int = 30) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise GuardError("command_unavailable") from None
    require(result.returncode == 0, "command_failed")
    return result.stdout


def object_json(value: str) -> dict:
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        raise GuardError("invalid_json") from None
    require(isinstance(result, dict), "invalid_json_shape")
    return result


def output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a") as stream:
            for key, value in values.items():
                require("\n" not in value and "\r" not in value, "invalid_output")
                stream.write(f"{key}={value}\n")


def validate_inputs(ref: str, mode: str, candidate: str, image: str, verify: str) -> None:
    require(ref == "refs/heads/dev", "trusted_dev_ref_required")
    require(mode in {"existing", "inspect", "candidate"}, "invalid_mode")
    require(verify in {"true", "false"}, "invalid_verify_flag")
    if mode == "candidate":
        require(valid_sha(candidate), "invalid_candidate_sha")
        require(not image and verify == "false", "conflicting_inputs")
    else:
        require(not candidate, "conflicting_inputs")
        if mode == "inspect":
            require(not image and verify == "false", "conflicting_inputs")
        else:
            require(not (image and verify == "true"), "conflicting_inputs")
            require(not image or (image.startswith("dev-") and valid_sha(image[4:])), "invalid_dev_image")


def github_commit(ref: str) -> str:
    repo = os.environ["GITHUB_REPOSITORY"]
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is not None, "invalid_repository")
    sha = object_json(run(["gh", "api", f"repos/{repo}/commits/{ref}"])).get("sha")
    require(valid_sha(sha), "invalid_commit_response")
    return sha


def verify_source(candidate: str) -> str:
    require(valid_sha(candidate), "invalid_candidate_sha")
    base = github_commit("dev")
    require(run(["git", "rev-parse", "origin/dev"]).strip() == base, "dev_changed_since_checkout")
    require(github_commit(candidate) == candidate, "candidate_not_in_repository")
    # fetch-depth:0 trusted checkout supplies all advertised branch objects. Require a
    # pushed, reachable candidate; an unavailable object fails without an authenticated fetch.
    run(["git", "cat-file", "-e", f"{candidate}^{{commit}}"])
    run(["git", "merge-base", "--is-ancestor", base, candidate])
    output(candidate_sha=candidate, base_sha=base)
    return base


def verify_drift(base: str) -> None:
    require(valid_sha(base) and github_commit("dev") == base, "dev_changed_since_validation")


def aws(*args: str) -> dict:
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_REF") == "refs/heads/dev"
            and os.environ.get("ROLE_ARN") == DEV_ROLE, "project_workflow_required")
    return object_json(run(["aws", *args, "--region", REGION, "--output", "json"]))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def open_health(url: str):
    require(url == f"{DEV_URL}/health", "fixed_dev_health_required")
    return build_opener(NoRedirect()).open(url, timeout=10)


def health(url: str) -> dict:
    try:
        with open_health(url) as response:
            require(response.status == 200, "public_health_failed")
            body = response.read(8193)
        require(len(body) <= 8192, "public_health_oversized")
        value = object_json(body.decode("utf-8"))
    except GuardError:
        raise
    except Exception:
        raise GuardError("public_health_failed") from None
    require(value.get("status") == "ok" and valid_sha(value.get("version")), "public_health_invalid")
    return {"status": "ok", "version": value["version"]}


def discover() -> tuple[dict[str, int], list[str]]:
    data = aws("ec2", "describe-instances", "--filters", "Name=tag:Role,Values=moly-backend-dev")
    counts: dict[str, int] = {}
    running = []
    for reservation in data.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            state = instance.get("State", {}).get("Name")
            require(state in {"pending", "running", "shutting-down", "terminated", "stopping", "stopped"},
                    "invalid_instance_state")
            counts[state] = counts.get(state, 0) + 1
            if state == "running":
                identity = instance.get("InstanceId")
                require(isinstance(identity, str) and re.fullmatch(r"i-[0-9a-f]{8,17}", identity) is not None,
                        "invalid_instance_identity")
                running.append(identity)
    return counts, running


MARKER_GUARD = 'test "$(cat /etc/moly-env 2>/dev/null)" = dev || { echo DEV_MARKER_REQUIRED; exit 41; }'
PROBE_PYTHON = """import json, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
phase=42
try:
    from app.config import settings
    phase=43
    opener=urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8000/health', timeout=5) as r:
        data=json.load(r)
        version=data.get('version')
    phase=44
    with opener.open('http://127.0.0.1:8000/health/ready', timeout=5) as r:
        ready=r.status == 200
    phase=45
    flags={key:getattr(settings,key) for key in ('context_checkpoint_enabled','current_turn_context_enabled')}
    print(json.dumps({'version':version,'ready':ready,'development':settings.environment == 'development','flags':flags}))
except Exception:
    print('PROBE_FAILED')
    raise SystemExit(phase)
"""


def remote_failure_code(state: dict) -> str:
    """Classify only enumerated SSM metadata; never inspect remote output bodies."""
    status = state.get("Status")
    if type(status) is not str or status not in {"Failed", "Cancelled", "TimedOut", "Cancelling"}:
        status = "unknown"
    response_code = state.get("ResponseCode")
    if type(response_code) is not int or response_code not in {41, 42, 43, 44, 45, 125, 126, 127}:
        response_code = "unknown"
    return f"remote_command_failed status={status} exit={response_code}"


def send_command(instance: str, command: str, *, seconds: int = 60) -> dict:
    require(re.fullmatch(r"i-[0-9a-f]{8,17}", instance) is not None, "invalid_instance_identity")
    result = aws("ssm", "send-command", "--instance-ids", instance,
                 "--document-name", "AWS-RunShellScript", "--parameters",
                 json.dumps({"commands": [command], "executionTimeout": [str(seconds)]}))
    command_id = result.get("Command", {}).get("CommandId")
    require(isinstance(command_id, str) and re.fullmatch(r"[0-9a-f-]{36}", command_id) is not None,
            "invalid_command_identity")
    for _ in range(seconds // 2 + 5):
        time.sleep(2)
        try:
            state = aws("ssm", "get-command-invocation", "--command-id", command_id,
                        "--instance-id", instance)
        except GuardError:
            continue  # Invocation registration can lag. A bounded final failure remains visible.
        status = state.get("Status")
        if status == "Success":
            return state
        require(type(status) is str and status in {"Pending", "InProgress", "Delayed"},
                remote_failure_code(state))
    raise GuardError("remote_command_timeout")


def host_probe(instance: str) -> dict:
    import shlex
    command = f"set -eu\n{MARKER_GUARD}\ndocker exec moly-backend python -c {shlex.quote(PROBE_PYTHON)}"
    state = send_command(instance, command)
    value = object_json(state.get("StandardOutputContent", ""))
    require(valid_sha(value.get("version")), "host_sha_invalid")
    require(value.get("ready") is True and value.get("development") is True, "host_not_ready_dev")
    flags = value.get("flags")
    require(isinstance(flags, dict) and all(type(flags.get(key)) is bool for key in FLAGS), "host_flags_invalid")
    return {"version": value["version"], "ready": True, "development": True,
            "flags": {key: flags[key] for key in FLAGS}}


def require_image(tag: str) -> None:
    require(tag.startswith("dev-") and valid_sha(tag[4:]), "invalid_dev_image")
    result = aws("ecr", "describe-images", "--repository-name", REPOSITORY,
                 "--image-ids", f"imageTag={tag}")
    details = result.get("imageDetails", [])
    require(isinstance(details, list) and len(details) == 1
            and tag in details[0].get("imageTags", []), "rollback_image_missing")


def inspect(candidate: bool) -> None:
    counts, running = discover()
    print(json.dumps({"dev_tag_state_counts": counts}))
    public = health(f"{DEV_URL}/health")
    print(json.dumps({"public": public}))
    if len(running) != 1:
        require(not candidate, "exactly_one_running_dev_required")
        return
    host = host_probe(running[0])
    print(json.dumps({"host": host, "public_host_sha_match": public["version"] == host["version"]}))
    if candidate:
        require(public["version"] == host["version"], "public_host_sha_mismatch")
        rollback = f"dev-{host['version']}"
        require_image(rollback)
        output(instance_id=running[0], rollback_tag=rollback)


def deploy(candidate: str, instance: str, rollback: str) -> None:
    require(valid_sha(candidate), "invalid_candidate_sha")
    require_image(rollback)
    _, running = discover()
    require(running == [instance], "dev_target_changed")
    host = host_probe(instance)
    require(host["version"] == rollback[4:] and health(f"{DEV_URL}/health")["version"] == host["version"],
            "dev_image_changed_since_validation")
    require_image(f"dev-{candidate}")
    # Existing infra deployment is retained. Suppress its possibly sensitive output;
    # remote failure yields a fixed diagnostic, never raw SSM stdout/stderr.
    command = (f"set -eu\n{MARKER_GUARD}\n"
               f"cd /root/moly-infra\ngit pull --ff-only >/dev/null 2>&1\n"
               f"sudo bash deploy.sh dev-{candidate} >/dev/null 2>&1\n")
    send_command(instance, command, seconds=600)
    for _ in range(6):
        try:
            if health(f"{DEV_URL}/health")["version"] == candidate:
                live = host_probe(instance)
                if live["version"] == candidate:
                    print(json.dumps({"deployed_sha": candidate, "rollback_tag": rollback,
                                      "host": live}))
                    return
        except GuardError:
            pass
        time.sleep(5)
    raise GuardError("deployed_sha_and_readiness_not_observed")


def main() -> None:
    action = sys.argv[1]
    if action == "inputs":
        validate_inputs(os.environ["GITHUB_REF"], os.environ["DEPLOY_MODE"],
                        os.environ.get("CANDIDATE_SHA", ""), os.environ.get("IMAGE_TAG", ""),
                        os.environ.get("VERIFY_ONLY", "false"))
    elif action == "source":
        verify_source(os.environ["CANDIDATE_SHA"])
    elif action == "drift":
        verify_drift(os.environ["BASE_SHA"])
    elif action == "inspect":
        inspect(os.environ["DEPLOY_MODE"] == "candidate")
    elif action == "deploy":
        verify_drift(os.environ["BASE_SHA"])
        deploy(os.environ["CANDIDATE_SHA"], os.environ["DEV_INSTANCE"], os.environ["ROLLBACK_TAG"])
    else:
        raise GuardError("invalid_action")


if __name__ == "__main__":
    try:
        main()
    except GuardError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("ERROR: validation_unavailable", file=sys.stderr)
        raise SystemExit(1) from None
