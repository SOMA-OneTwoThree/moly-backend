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


async def _tick_then_flush() -> dict[str, int]:
    from app.services import usage_ledger

    try:
        return await run_tick(datetime.now(timezone.utc))
    finally:
        # #23b: 이 프로세스는 단명(틱 1회)이라 flusher 태스크가 없다 — 여기서 마지막으로
        # 원장 close 버퍼를 민다. 없으면 04시 일기 생성 LLM 비용이 매일 'started'로 남아
        # 24h 뒤 unknown_usage(상한 추정)로만 수렴한다(실측 토큰·비용 소실).
        await usage_ledger.flush_closes()


def main() -> None:
    counts = asyncio.run(_tick_then_flush())
    _log.info(
        "tick 완료 — 일기 %d · 아침 %d · 저녁 %d",
        counts["diaries"], counts["morning"], counts["evening"],
    )


if __name__ == "__main__":
    main()
