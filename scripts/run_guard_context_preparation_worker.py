"""Run existing memory/content workers during the closed all-user preparation window.

No notification/critical/maintenance queue or global expiry cleanup is run.
SIGTERM drains in-flight work; existing usage-ledger finalization is preserved.
This command neither changes feature switches nor opens the API.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from urllib.parse import urlsplit
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.envfile import bootstrap_script_environment, configure_application_db, project_ref


def validate_target(dsn: str, expected_ref: str, maintenance_confirmed: bool) -> None:
    target = urlsplit(dsn)
    host = target.hostname or ""
    if (target.scheme not in {"postgres", "postgresql"} or target.path != "/postgres"
            or target.port not in {5432, 6543}
            or not (host.endswith(".pooler.supabase.com") or host == f"db.{expected_ref}.supabase.co")):
        raise ValueError("preparation_target_mismatch")
    if expected_ref not in {"wywzjslvxwttxkecbyis", "qkgjlgzsharnilxnkytd"} or project_ref(dsn) != expected_ref:
        raise ValueError("preparation_target_mismatch")
    if expected_ref == "qkgjlgzsharnilxnkytd" and not maintenance_confirmed:
        raise ValueError("closed_maintenance_window_required")


async def run() -> None:
    from worker.consumer import run_consumer

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await run_consumer(queues=("memory", "content"), stop=stop, global_cleanup=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--expected-project-ref", required=True)
    parser.add_argument("--maintenance-confirmed", action="store_true")
    args = parser.parse_args()
    bootstrap_script_environment(["--env", args.env])
    from db.envfile import load_conn

    try:
        validate_target(load_conn(args.env), args.expected_project_ref,
                        args.maintenance_confirmed)
        configure_application_db(args.env)
        asyncio.run(run())
    except Exception as exc:
        print(f"preparation_worker_failed:{type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
