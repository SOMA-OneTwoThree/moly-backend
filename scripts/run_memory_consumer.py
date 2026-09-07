"""선택한 환경의 memory 큐를 제한된 시간 동안 수동 처리한다.

현재 배포된 consumer와 같은 처리기를 사용한다. 해당 큐의 모든 사용자 작업이 대상이며
외부 LLM·벡터 호출과 비용이 발생한다. --seconds 후 새 claim을 멈추고 실행 중 작업을 마친다.
--until-empty도 이 시간 제한 안에서만 대기한다. 실패 작업이 있으면 종료 코드 1이다.

    PYTHONPATH=. uv run python scripts/run_memory_consumer.py --env dev --seconds 60
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
_log = logging.getLogger("memcons")

QUEUE = "memory"


async def _pending(session_maker) -> tuple[int, int, int]:
    from app.services import jobs

    async with session_maker() as s:
        # 4a47960: a successful replay resolves every dead ancestor, while the
        # original rows remain for audit. Use the production queue definition.
        stats = (await jobs.queue_stats(s))[QUEUE]
    return stats['ready'], stats['running'], stats['dead']


async def main() -> int:
    from db.envfile import announce, bootstrap_script_environment, configure_application_db, split_env_arg

    bootstrap_script_environment(sys.argv[1:])
    env, rest = split_env_arg(sys.argv[1:])
    env = env or os.environ['MOLY_ENV_FILE']
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=60, help="새 작업 claim을 멈출 때까지의 최대 초")
    ap.add_argument("--until-empty", action="store_true", help="큐가 빌 때까지 돈다")
    args = ap.parse_args(rest)
    if not 0 < args.seconds < float('inf'):
        ap.error('seconds must be finite and positive')
    announce(env, configure_application_db(env), commit=True)

    from app.core.db import get_sessionmaker  # noqa: E402
    from worker import consumer  # noqa: E402

    maker = get_sessionmaker()
    ready, running, dead = await _pending(maker)
    _log.info("시작 전 — 대기 %d · 진행 %d · 실패 %d", ready, running, dead)
    if ready == 0 and running == 0:
        _log.info("처리할 작업이 없다. 먼저 enter_shadow_cohort.py 로 등록한다.")
        return int(dead > 0)
    if dead:
        _log.error("기존 실패 작업을 먼저 검토한다.")
        return 1

    stop = asyncio.Event()
    task = asyncio.ensure_future(consumer.run_consumer(queues=(QUEUE,), stop=stop))

    async def watch() -> None:
        idle = 0
        while not stop.is_set():
            await asyncio.sleep(10)
            r, run_, d = await _pending(maker)
            _log.info("  대기 %d · 진행 %d · 실패 %d", r, run_, d)
            if d:
                stop.set()
            if args.until_empty:
                idle = idle + 1 if (r == 0 and run_ == 0) else 0
                if idle >= 2:  # 20초 연속 비어 있으면 끝
                    _log.info("큐가 비었다 — 종료한다.")
                    stop.set()

    watcher = asyncio.ensure_future(watch())
    try:
        done, _ = await asyncio.wait({task, watcher}, timeout=args.seconds,
                                     return_when=asyncio.FIRST_COMPLETED)
        for finished in done:
            finished.result()
        stop.set()
        await task
    finally:
        stop.set()
        watcher.cancel()
        # A monitor exception was already propagated above. It must not skip the
        # consumer drain in this finally block and abandon an external write.
        await asyncio.gather(watcher, return_exceptions=True)
        if not task.done():
            await task
        r, run_, d = await _pending(maker)
        _log.info("끝 — 대기 %d · 진행 %d · 실패 %d", r, run_, d)
    return int(d > 0 or (args.until_empty and r + run_ > 0))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
