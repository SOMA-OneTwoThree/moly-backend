"""Dedicated live-preparation worker. Public readers/workers stay online.

Only exact building-preparation contract/checkpoint jobs are claimable. No global
reaper, expiry cleanup, notification or public fact-pipeline job runs here.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path
import signal
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def validate_target(dsn: str, expected_ref: str, production_ack: bool) -> None:
    from scripts.prepare_guard_context_release import require_target
    require_target("prod" if expected_ref == "qkgjlgzsharnilxnkytd" else "dev",
                   dsn, expected_ref, production_ack)


async def run(preparation_id: uuid.UUID, *, expected_code_sha: str) -> None:
    from app.config import settings
    from app.core.db import get_sessionmaker
    from app.services import retention_release_promotion as promotion
    from app.services.retention_preparation_context import Binding, use_binding
    from worker.consumer import run_consumer
    if not promotion.valid_sha(expected_code_sha) or settings.git_sha != expected_code_sha:
        raise promotion.Blocked("running_code_sha_mismatch")
    async with get_sessionmaker()() as session:
        row = await promotion.registry(session, preparation_id)
        if row["state"] not in {"building", "prepared"} or row["code_sha"] != expected_code_sha:
            raise promotion.Blocked("building_preparation_required")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    with use_binding(Binding(preparation_id, True)):
        await run_consumer(queues=("memory", "content"), stop=stop, global_cleanup=False,
                           reap=False, preparation_id=preparation_id, concurrency_cap=1)


def main() -> int:
    from db.envfile import bootstrap_script_environment, configure_application_db, load_conn
    from scripts.prepare_guard_context_release import private_env
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=("dev", "prod"), default="dev")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--expected-project-ref", required=True)
    parser.add_argument("--expected-code-sha", required=True)
    parser.add_argument("--preparation-id", type=uuid.UUID, required=True)
    parser.add_argument("--production-ack", action="store_true")
    args = parser.parse_args()
    try:
        env = private_env(args)
        bootstrap_script_environment(["--env", env])
        validate_target(load_conn(env), args.expected_project_ref, args.production_ack)
        configure_application_db(env)
        asyncio.run(run(args.preparation_id, expected_code_sha=args.expected_code_sha))
    except Exception as exc:
        print(f"preparation_worker_failed:{type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
