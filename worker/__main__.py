"""배치 워커 엔트리포인트 — API와 같은 코드베이스, 프로세스만 분리(ARCHITECTURE §3.3).

외부 매시 크론이 `python -m worker` 1틱 실행(멱등). 04:00 일기 생성·기억통합 / 09:00·20:00 푸시.
dev 수동 테스트는 `python -m worker --diary-for YYYY-MM-DD`로 일기 생성 시각을 위조할 수 있다.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from worker.tick import run_tick

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("moly-worker")


def _tick_time(diary_for: str | None) -> datetime:
    """평시엔 현재 시각. --diary-for가 있으면 그 날짜(KST) 대화분의 일기 생성이
    발동하는 시각 — 다음날 KST 04:30 — 으로 위조한다.

    04:30인 이유: 04:00 정각은 activity_date의 하루 경계(DAY_BOUNDARY_HOUR=4)와
    겹쳐서 경계 직전/직후 판정이 갈릴 수 있다. 30분 안쪽이면 hour==4 조건은
    그대로 만족하면서 경계 모호함이 사라진다.
    """
    if diary_for is None:
        return datetime.now(timezone.utc)
    target = date.fromisoformat(diary_for)
    kst_0430 = datetime.combine(
        target + timedelta(days=1), time(4, 30), tzinfo=ZoneInfo("Asia/Seoul")
    )
    return kst_0430.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="배치 워커 1틱(멱등) 실행")
    parser.add_argument(
        "--diary-for",
        metavar="YYYY-MM-DD",
        default=None,
        help="이 날짜(KST) 대화분의 일기 생성 틱으로 실행 — dev 테스트용. "
        "미지정 시 현재 시각(평시 크론 동작). KST가 아닌 타임존 유저에겐 발동하지 않을 수 있다.",
    )
    args = parser.parse_args()

    now = _tick_time(args.diary_for)
    if args.diary_for:
        _log.info("일기 테스트 모드 — %s 대화분, 위조 시각 %s", args.diary_for, now.isoformat())
    counts = asyncio.run(run_tick(now))
    _log.info(
        "tick 완료 — 일기 %d · 아침 %d · 저녁 %d",
        counts["diaries"], counts["morning"], counts["evening"],
    )


if __name__ == "__main__":
    main()
