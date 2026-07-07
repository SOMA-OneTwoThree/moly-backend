"""배치 워커 엔트리포인트 — API와 같은 코드베이스, 프로세스만 분리(ARCHITECTURE §3.3).

외부 매시 크론이 `python -m worker` 1틱 실행(멱등). 현재 = 04:00 일기 생성 + 기억 통합.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from worker.tick import run_tick

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("moly-worker")


def main() -> None:
    processed = asyncio.run(run_tick(datetime.now(timezone.utc)))
    _log.info("diary tick 완료 — 처리 유저 %d명", processed)


if __name__ == "__main__":
    main()
