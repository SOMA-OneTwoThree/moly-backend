"""Trusted development deployment controls. Never print subprocess or HTTP bodies."""
from __future__ import annotations

import inspect as source_inspect
import json
import math
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

# Read-only inspect contract. These are approved route/catalog IDs, not arbitrary
# slugs; an unknown setting must never become diagnostic output.
RUNTIME_MODELS = (
    "gpt-4.1-mini-2025-04-14", "gpt-5.6-luna", "gpt-5.6-terra",
    "gpt-6-luna", "gpt-6-sol",
)
RUNTIME_SDK = {"openai": "2.44.0", "anthropic": "0.116.0"}
RUNTIME_CONTAINERS = {"api": "moly-backend", "consumer": "moly-consumer"}
RUNTIME_FIELDS = {
    "api": (
        "git_sha", "environment", "context_checkpoint_enabled", "current_turn_context_enabled",
        "model_chat", "embedder_model", "llm_max_tokens",
    ),
    "consumer": (
        "git_sha", "environment", "context_checkpoint_enabled", "model_utility",
        "llm_timeout_s", "job_content_timeout_s", "job_content_lease_s",
        "job_content_heartbeat_s", "job_content_max_attempts", "job_finalize_timeout_s",
    ),
}
RUNTIME_ERRORS = frozenset({
    "runtime_unavailable", "runtime_shape_invalid", "model_unapproved", "sdk_unapproved",
    "runtime_limits_invalid", "identity_drift", "container_unavailable",
})


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
    require(verify == "false", "legacy_verification_removed")
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


def _runtime_json(raw: str) -> dict:
    """Success bodies are bounded and exact; errors never contain their content."""
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("runtime_shape_invalid")
            value[key] = item
        return value

    if type(raw) is not str or len(raw.encode("utf-8")) > 8192:
        raise ValueError("runtime_shape_invalid")
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError):
        raise ValueError("runtime_shape_invalid") from None
    if type(value) is not dict:
        raise ValueError("runtime_shape_invalid")
    return value


def _runtime_settings(role: str, value: dict) -> dict:
    fields = RUNTIME_FIELDS[role]
    if type(value) is not dict or set(value) != set(fields) | {"sdk"}:
        raise ValueError("runtime_shape_invalid")
    if (type(value["git_sha"]) is not str or re.fullmatch(r"[0-9a-f]{40}", value["git_sha"]) is None
            or value["environment"] != "development"):
        raise ValueError("identity_drift")
    flags = ("context_checkpoint_enabled", "current_turn_context_enabled") if role == "api" else ("context_checkpoint_enabled",)
    if any(type(value[key]) is not bool for key in flags):
        raise ValueError("runtime_shape_invalid")
    model_key = "model_chat" if role == "api" else "model_utility"
    if type(value[model_key]) is not str or value[model_key] not in RUNTIME_MODELS:
        raise ValueError("model_unapproved")
    if role == "api" and value["embedder_model"] != "text-embedding-3-small":
        raise ValueError("model_unapproved")
    sdk = value["sdk"]
    if type(sdk) is not dict or set(sdk) != set(RUNTIME_SDK) or any(
        type(sdk[key]) is not str or sdk[key] != expected for key, expected in RUNTIME_SDK.items()
    ):
        raise ValueError("sdk_unapproved")

    def integer(key, maximum):
        item = value[key]
        if type(item) is not int or not 1 <= item <= maximum:
            raise ValueError("runtime_limits_invalid")

    if role == "api":
        integer("llm_max_tokens", 32768)
    else:
        integer("job_content_max_attempts", 32)
        for key in ("llm_timeout_s", "job_content_timeout_s", "job_content_lease_s",
                    "job_content_heartbeat_s", "job_finalize_timeout_s"):
            item = value[key]
            if type(item) not in (int, float) or not math.isfinite(item) or not 0 < item <= 86400:
                raise ValueError("runtime_limits_invalid")
        timeout, lease = value["job_content_timeout_s"], value["job_content_lease_s"]
        if (value["llm_timeout_s"] > timeout
                or timeout + value["job_finalize_timeout_s"] >= lease
                or value["job_content_heartbeat_s"] > min(lease / 3, 20)):
            raise ValueError("runtime_limits_invalid")
    return {**{key: value[key] for key in fields}, "sdk": dict(RUNTIME_SDK)}


def _runtime_image(value: dict) -> dict:
    if type(value) is not dict or set(value) != {"image", "running"}:
        raise ValueError("runtime_shape_invalid")
    if (type(value["image"]) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value["image"]) is None):
        raise ValueError("identity_drift")
    if type(value["running"]) is not bool or not value["running"]:
        raise ValueError("container_unavailable")
    return {"image": value["image"], "running": True}


def _runtime_collect() -> dict:
    """Runs on the dev host. Capture child output privately; emit only validated values."""
    def command(args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise ValueError("container_unavailable")
        return _runtime_json(result.stdout)

    def identities():
        return {role: _runtime_image(command([
            "docker", "inspect", "--format",
            '{"image":{{json .Image}},"running":{{json .State.Running}}}', container,
        ])) for role, container in RUNTIME_CONTAINERS.items()}

    before = identities()
    values = {}
    for role, container in RUNTIME_CONTAINERS.items():
        # No environment/object dump. A bad model value remains inside the captured
        # pipe and fails validation before it can reach SSM stdout.
        program = (
            "import json; from importlib.metadata import version; from app.config import settings; "
            f"value={{key:getattr(settings,key) for key in {RUNTIME_FIELDS[role]!r}}}; "
            "value['sdk']={key:version(key) for key in ('openai','anthropic')}; print(json.dumps(value))"
        )
        values[role] = _runtime_settings(role, command(["docker", "exec", container, "python", "-c", program]))
    after = identities()
    if (before != after or before["api"]["image"] != before["consumer"]["image"]
            or values["api"]["git_sha"] != values["consumer"]["git_sha"]):
        raise ValueError("identity_drift")
    return {"containers": after, "settings": values}


def runtime_probe(instance: str, expected_sha: str) -> dict:
    """Additional diagnostics only for explicit inspect; never a deployment gate."""
    import shlex

    require(valid_sha(expected_sha), "invalid_runtime_expected_sha")
    # Send the same validators that validate the returned JSON locally. No code,
    # container name or model is supplied by workflow input.
    program = "import json, math, re, subprocess\n"
    for name in ("RUNTIME_MODELS", "RUNTIME_SDK", "RUNTIME_CONTAINERS", "RUNTIME_FIELDS", "RUNTIME_ERRORS"):
        program += f"{name} = {globals()[name]!r}\n"
    for fn in (_runtime_json, _runtime_settings, _runtime_image, _runtime_collect):
        program += source_inspect.getsource(fn) + "\n"
    program += (
        "try:\n    print(json.dumps({'status':'available', **_runtime_collect()}))\n"
        "except Exception as error:\n"
        "    code=str(error) if type(error) is ValueError and str(error) in RUNTIME_ERRORS else 'runtime_unavailable'\n"
        "    print(json.dumps({'status':'unavailable','reason':code}))\n"
    )
    command = f"set -eu\n{MARKER_GUARD}\npython3 -c {shlex.quote(program)}"
    try:
        state = send_command(instance, command, seconds=90)
        value = _runtime_json(state.get("StandardOutputContent", ""))
        if set(value) == {"status", "reason"} and value["status"] == "unavailable":
            if type(value["reason"]) is str and value["reason"] in RUNTIME_ERRORS:
                return {"status": "unavailable", "reason": value["reason"]}
            raise ValueError("runtime_shape_invalid")
        if (set(value) != {"status", "containers", "settings"} or value["status"] != "available"
                or type(value["containers"]) is not dict or set(value["containers"]) != set(RUNTIME_CONTAINERS)
                or type(value["settings"]) is not dict or set(value["settings"]) != set(RUNTIME_CONTAINERS)):
            raise ValueError("runtime_shape_invalid")
        containers = {role: _runtime_image(value["containers"][role]) for role in RUNTIME_CONTAINERS}
        settings = {role: _runtime_settings(role, value["settings"][role]) for role in RUNTIME_CONTAINERS}
        if (containers["api"]["image"] != containers["consumer"]["image"]
                or any(s["git_sha"] != expected_sha for s in settings.values())):
            raise ValueError("identity_drift")
        # Recheck API readiness and public SHA after both container snapshots.
        host_after = host_probe(instance)
        public_after = health(f"{DEV_URL}/health")
        if (host_after["version"] != expected_sha or public_after["version"] != expected_sha
                or any(host_after["flags"][key] != settings["api"][key] for key in FLAGS)):
            raise ValueError("identity_drift")
        return {"status": "available", "containers": containers, "settings": settings,
                "utility_provider": "openai"}
    except ValueError as error:
        code = str(error) if str(error) in RUNTIME_ERRORS else "runtime_shape_invalid"
        return {"status": "unavailable", "reason": code}
    except Exception:
        return {"status": "unavailable", "reason": "runtime_unavailable"}


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
        print(json.dumps({"runtime": {"status": "unavailable", "reason": "target_count"}}))
        return
    host = host_probe(running[0])
    print(json.dumps({"host": host, "public_host_sha_match": public["version"] == host["version"]}))
    if candidate:
        require(public["version"] == host["version"], "public_host_sha_mismatch")
        rollback = f"dev-{host['version']}"
        require_image(rollback)
        output(instance_id=running[0], rollback_tag=rollback)
    else:
        runtime = (runtime_probe(running[0], host["version"])
                   if public["version"] == host["version"]
                   else {"status": "unavailable", "reason": "identity_drift"})
        print(json.dumps({"runtime": runtime}))


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
