"""배치 워커 엔트리포인트 — API와 같은 코드베이스, 프로세스만 분리(ARCHITECTURE §3.3).

1시간 틱 크론: 타임존별 04:00(일기 생성·기억통합)·09:00/21:00(APNs 푸시) 지난 유저 스캔.
구현은 배치 설계 단계에서 — 지금은 부팅 확인용 스텁.
"""
from __future__ import annotations

import logging

from app.config import settings

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("moly-worker")


def main() -> None:
    _log.info(
        "moly-worker 부팅 (app=%s env=%s) — 배치 로직 미구현(스텁)",
        settings.app_name,
        settings.environment,
    )


if __name__ == "__main__":
    main()
