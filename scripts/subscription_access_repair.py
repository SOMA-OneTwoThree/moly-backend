"""Dev-only, explicit one-customer repair. Private previews must stay outside Git.

Production changes deliberately have no execution flag in this tool. Review the
deployment and SQL procedures separately before implementing a production entry point.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import bootstrap_script_environment

bootstrap_script_environment(sys.argv[1:])


def reserve_private(path: Path):
    if any((parent / ".git").exists() for parent in path.resolve().parents):
        raise ValueError("private customer preview must be outside the repository")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
    return os.fdopen(fd, "a")


def private_write(stream, value: dict):
    # One durable JSON line per stage; a later failed write cannot truncate the
    # last known commit state. A preview has a single line and remains valid JSON.
    json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())


async def run(args):
    with reserve_private(args.output) as audit_file:
        await _run(args, audit_file)


async def _run(args, audit_file):
    import httpx
    from app.core.db import get_sessionmaker
    from app.services import subscription_repair as repair
    from db.envfile import assert_dev_target, configure_application_db, project_ref

    # Validate and reserve private output before any possible database mutation.
    if args.action == "apply":
        private_write(audit_file, {"status": "not_applied", "action": args.action})
    dsn = configure_application_db(args.env)
    assert_dev_target(args.env, dsn)
    if len(args.expected_app_sha) != 40 or any(c not in "0123456789abcdef" for c in args.expected_app_sha):
        raise ValueError("full deployed application SHA is required")
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.get("https://dev.moly.asia/health")
        response.raise_for_status()
        body = response.json()
        if body.get("version") != args.expected_app_sha or body.get("env") != "development":
            raise ValueError("development deployment SHA mismatch")
    async with get_sessionmaker()() as session:
        try:
            if args.action == "preview":
                plan = await repair.preview(session, uuid.UUID(args.subscription_id), args.mode,
                                            project_ref=project_ref(dsn), app_sha=args.expected_app_sha)
                private_write(audit_file, plan)
                print("preview_sha256=" + repair.digest(plan))
            else:
                plan = json.loads(args.plan.read_text())
                audit = await repair.apply_preview(session, plan, expected_sha256=args.expected_sha256,
                                                   project_ref=project_ref(dsn), app_sha=args.expected_app_sha,
                                                   on_status=lambda value: private_write(audit_file, value))
                if not audit["postcheck_provider_unchanged"]:
                    raise ValueError("repair committed; provider changed; stop and re-plan, do not restore old state")
                print("repair_applied_and_verified")
        except BaseException:
            await session.rollback()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "apply"))
    parser.add_argument("--env", choices=("dev",), default="dev")
    parser.add_argument("--expected-app-sha", required=True)
    parser.add_argument("--subscription-id")
    parser.add_argument("--mode", choices=("refunded", "metadata"))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if (args.action == "preview" and not (args.subscription_id and args.mode)) or (
        args.action == "apply" and not (args.plan and args.expected_sha256)
    ):
        parser.error("preview requires subscription-id/mode; apply requires plan/expected-sha256")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
