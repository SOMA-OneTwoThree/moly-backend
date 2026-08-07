"""기억 큐 전용 소비자 — 운영 전환 1.5단계에서 사람이 손으로 돌린다(1회성).

배포 전에 과거 대화에서 장기기억을 미리 만들어 두기 위한 것이다. 운영 서버는 아직 구 코드라
`async_jobs`를 모르므로, 이 코드를 사람이 자기 컴퓨터에서 돌려 큐를 비운다.

**기억 큐만 돌린다.** `worker.consumer.run_consumer(queues=...)`에 `memory`만 넘긴다.
그 큐에 들어가는 작업은 `mem0_ingest`·`mem0_consolidate`·`reconsolidate` 셋뿐이고
(`app/services/memory_pipeline.py`가 넣는 세 곳이 전부), 그 처리기들이 쓰는 표는
`mem0_*`·`memory_pipeline_states`·`relationship_events`로 전부 새 표다. 구 코드가 쓰는
`messages`·`chat_contexts`·`diaries`·`profiles`는 읽기만 한다. 벡터도 `moly_memories_v2`라
구 코드의 `memories`와 분리돼 있다.

접속 대상은 **`MOLY_ENV_FILE`이 정한다.** `--env`는 화면 표시만 바꾸는 스크립트가 있으니
반드시 `MOLY_ENV_FILE`을 준다.

사용:
    MOLY_ENV_FILE=.env.prod PYTHONPATH=. uv run python scripts/run_memory_consumer.py --seconds 60
    MOLY_ENV_FILE=.env.prod PYTHONPATH=. uv run python scripts/run_memory_consumer.py --until-empty
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
    from sqlalchemy import text

    async with session_maker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT count(*) FILTER (WHERE state='ready') ready,"
                    "       count(*) FILTER (WHERE state='running') running,"
                    "       count(*) FILTER (WHERE state='dead') dead"
                    "  FROM async_jobs WHERE queue=:q"
                ),
                {"q": QUEUE},
            )
        ).first()
    return int(row[0]), int(row[1]), int(row[2])


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0, help="이 시간만 돌고 멈춘다")
    ap.add_argument("--until-empty", action="store_true", help="큐가 빌 때까지 돈다")
    args = ap.parse_args()

    envf = os.getenv("MOLY_ENV_FILE", ".env")
    from app.config import settings  # noqa: E402

    dsn = settings.supabase_db_connection_string
    ref = dsn.split("@")[-1].split(".")[0] if "@" in dsn else "?"
    _log.info("접속 대상 env=%s (project 주소 앞부분 %s)", envf, ref[:14])

    from app.core.db import get_sessionmaker  # noqa: E402
    from worker import consumer  # noqa: E402

    maker = get_sessionmaker()
    ready, running, dead = await _pending(maker)
    _log.info("시작 전 — 대기 %d · 진행 %d · 실패 %d", ready, running, dead)
    if ready == 0 and running == 0:
        _log.info("처리할 작업이 없다. 먼저 enter_shadow_cohort.py 로 등록한다.")
        return

    stop = asyncio.Event()
    task = asyncio.ensure_future(consumer.run_consumer(queues=(QUEUE,), stop=stop))

    async def watch() -> None:
        idle = 0
        while not stop.is_set():
            await asyncio.sleep(10)
            r, run_, d = await _pending(maker)
            _log.info("  대기 %d · 진행 %d · 실패 %d", r, run_, d)
            if args.until_empty:
                idle = idle + 1 if (r == 0 and run_ == 0) else 0
                if idle >= 2:  # 20초 연속 비어 있으면 끝
                    _log.info("큐가 비었다 — 종료한다.")
                    stop.set()

    watcher = asyncio.ensure_future(watch())
    try:
        if args.seconds:
            await asyncio.sleep(args.seconds)
            stop.set()
        await task
    finally:
        stop.set()
        watcher.cancel()
        r, run_, d = await _pending(maker)
        _log.info("끝 — 대기 %d · 진행 %d · 실패 %d", r, run_, d)


if __name__ == "__main__":
    asyncio.run(main())
