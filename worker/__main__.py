"""배치 워커 엔트리포인트 — API와 같은 코드베이스, 프로세스만 분리(ARCHITECTURE §3.3).

외부 매시 크론이 `python -m worker` 1틱 실행(멱등). 04:00 일기 생성·기억통합 / 09:00·20:00 푸시.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from worker.tick import run_tick

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("moly-worker")


def main() -> None:
    counts = asyncio.run(run_tick(datetime.now(timezone.utc)))
    _log.info(
        "tick 완료 — 일기 %d · 아침 %d · 저녁 %d",
        counts["diaries"], counts["morning"], counts["evening"],
    )


if __name__ == "__main__":
    main()
