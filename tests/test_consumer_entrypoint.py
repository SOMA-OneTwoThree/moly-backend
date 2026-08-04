"""실제 `python -m worker.consumer` 진입점의 handler registry 회귀."""
import os
import subprocess
import sys
from pathlib import Path

from app.services import checkpoint, memory_repo


def test_module_entrypoint_registers_handlers_on_the_live_registry():
    env = {**os.environ, "MOLY_CONSUMER_STARTUP_CHECK_ONLY": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "worker.consumer"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    registered = set(result.stdout.strip().split(","))
    assert {
        checkpoint.JOB_CONVERSATION_CHECKPOINT,
        memory_repo.JOB_MEMORY_EXTRACT,
        memory_repo.JOB_MEMORY_RECONCILE,
        memory_repo.JOB_MEMORY_EMBED,
        memory_repo.JOB_PROFILE_REFRESH,
    } <= registered
