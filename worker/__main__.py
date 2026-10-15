"""배치 워커 엔트리포인트 — API와 같은 코드베이스의 별도 프로세스.

외부 스케줄러가 한 tick을 실행한다. 시간대별 작업은 worker/tick.py와 docs/ARCHITECTURE.md의 비동기 작업과 데이터 절을 따른다."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.core.logging_setup import configure_logging
from worker import synthetic_probe
from worker.tick import run_tick

configure_logging()  # API·consumer와 같은 JSON 포맷. httpx INFO(요청 URL 노출)는 끈다
_log = logging.getLogger("moly-worker")


async def _tick_then_flush() -> dict[str, int]:
    from app.services import usage_ledger

    # 합성 점검은 틱 본 작업과 동시에 돈다(API 건강 — 경보만, 데드맨과 분리).
    probe = synthetic_probe.start()
    try:
        return await run_tick(datetime.now(timezone.utc))
    finally:
        # #23b: 이 프로세스는 단명(틱 1회)이라 flusher 태스크가 없다 — 여기서 마지막으로
        # 원장 close 버퍼를 민다. 없으면 04시 일기 생성 LLM 비용이 매일 'started'로 남아
        # 24h 뒤 unknown_usage(상한 추정)로만 수렴한다(실측 토큰·비용 소실).
        try:
            await usage_ledger.flush_closes()
        finally:
            await synthetic_probe.finish(probe)


def main() -> None:
    counts = asyncio.run(_tick_then_flush())
    _log.info(
        "tick 완료 — 일기 %d · 아침 %d · 저녁 %d",
        counts["diaries"], counts["morning"], counts["evening"],
    )


if __name__ == "__main__":
    main()
