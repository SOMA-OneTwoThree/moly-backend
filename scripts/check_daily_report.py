"""Render the deployed daily report against its DB, read-only, without Slack sends."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.config import settings  # noqa: E402
from app.core import db  # noqa: E402
from worker import monitoring  # noqa: E402


async def check(environment: str, expected_sha: str, *, show: bool = False) -> None:
    if settings.environment != environment or settings.git_sha != expected_sha:
        raise ValueError("runtime mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("invalid version")
    if db._engine is not None:
        raise ValueError("engine already initialized")
    db._engine = create_async_engine(db._async_dsn(), pool_size=4, max_overflow=0,
        connect_args={"statement_cache_size": 0,
                      "server_settings": {"default_transaction_read_only": "on"}})
    started = monotonic()
    try:
        async with asyncio.timeout(monitoring._MONITORING_BUDGET_S):
            now = datetime.now(timezone.utc)
            async with db.get_sessionmaker()() as session:
                rc = await monitoring.rc_snapshot(session, now)
            message = await monitoring._daily_message(now, rc, persist_snapshot=False)
        for required in ("어제 신규 가입: ", "DAU: ", "MAU: ", "현재 구독 이용자: ", "개인 일기: 대상 "):
            if not re.search(re.escape(required)+r"[0-9]", message):
                raise ValueError("missing report metric")
        if any(fragment in message for fragment in (
            "집계 확인 필요", "집계 시간 초과", "집계 조회 실패", "상태 조회 확인 필요",
        )):
            raise ValueError("report query unavailable")
        print(json.dumps({"daily_report": "ok", "environment": environment, "version": expected_sha,
                          "read_only": True, "slack_sent": False,
                          "elapsed_ms": int((monotonic()-started)*1000)}))
        if show:
            print(message)
    finally:
        await db._engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=("development", "production"))
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--show", action="store_true", help="print aggregate report preview")
    args = parser.parse_args()
    try:
        asyncio.run(check(args.environment, args.expected_sha, show=args.show))
    except Exception as exc:
        print(json.dumps({"daily_report": "failed", "error": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
