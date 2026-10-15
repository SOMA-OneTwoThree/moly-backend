"""API 안 경보 — 로그로만 보이던 이상 신호를 경보 채널(slack_notify.alert)로 올린다.

- 요청 경로에서 부른다. 억제 판정은 바로 하고 전송만 태스크로 띄운다 → 응답 지연 0.
  전송·판정 실패는 로그만 남긴다(경보가 요청 실패로 번지면 안 된다).
- 억제 창: 미처리 예외는 alert_dedup_window_sec(5분), 회상 타임아웃은 집계 창(15분), 느린 턴은
  alert_slow_turn_dedup_s(15분). 오래 이어지는 지연이 경보를 쌓지 않게 둘은 길게 둔다. 프로세스마다 따로 센다.
- 문구에는 예외 종류·경로 템플릿·수치만 넣는다. 사용자 내용·user id·비밀값은 넣지 않는다.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import re
import time
from typing import Any

from app.config import settings
from app.services import slack_notify

_log = logging.getLogger("moly-backend")

# 띄운 전송 태스크 — 참조를 잡아 두지 않으면 끝나기 전에 GC될 수 있다.
_pending: set[asyncio.Task] = set()
# 최근 회상 타임아웃 시각(monotonic). 기록할 때마다 창 밖 항목을 버린다.
_recall_timeouts: collections.deque[float] = collections.deque()
# 경로 템플릿을 못 얻었을 때 id처럼 보이는 경로 조각을 가린다(UUID·hex·숫자).
_ID_SEGMENT = re.compile(r"/(?:[0-9A-Fa-f-]{16,}|\d+)(?=/|$)")


def fire(text: str, *, dedup_key: str, window: float | None = None) -> bool:
    """억제 창(기본 alert_dedup_window_sec) 밖이면 경보를 태스크로 띄우고 True.

    실행 중인 이벤트 루프가 없으면 보내지 않는다(억제 시각도 남기지 않는다).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if not slack_notify.claim(dedup_key, window):
        return False
    task = loop.create_task(_send(text))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return True


async def _send(text: str) -> None:
    try:
        await slack_notify.alert(text)
    except Exception:  # noqa: BLE001  # 경보 실패는 로그만
        _log.warning("앱 경보 전송 실패", exc_info=True)


def _route_label(request: Any) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None) or _ID_SEGMENT.sub("/{id}", request.url.path)
    return f"{request.method} {path}"


def unhandled(request: Any, exc: BaseException) -> None:
    """미처리 예외(500 INTERNAL) — 예외 종류별로 억제한다. 예외 메시지는 싣지 않는다(SQL 인자 등)."""
    try:
        kind = type(exc).__name__
        fire(
            f"🚨 API 미처리 예외 {kind} — {_route_label(request)} → 500. 로그 `unhandled error` 확인",
            dedup_key=f"unhandled:{kind}",
        )
    except Exception:  # noqa: BLE001
        _log.warning("미처리 예외 경보 판정 실패", exc_info=True)


def recall_timeout() -> None:
    """회상 타임아웃 1건 기록 — 창 안 누적이 하한 이상이면 경보(그 턴들은 빈 기억으로 답했다).

    보내면 집계를 비운다 — 경보 하나가 새 타임아웃 하한 수만큼이다. 억제 창은 집계 창과 같다.
    """
    try:
        threshold = settings.alert_recall_timeouts
        if threshold <= 0:
            return
        now = time.monotonic()
        window = settings.alert_recall_timeout_window_s
        _recall_timeouts.append(now)
        while _recall_timeouts and now - _recall_timeouts[0] > window:
            _recall_timeouts.popleft()
        if len(_recall_timeouts) >= threshold and fire(
            f"🐌 기억 회상 타임아웃 {len(_recall_timeouts)}회/{window // 60}분 — 그 턴들은 빈 기억으로 답함."
            " 로그 `v2 회상 타임아웃`의 stages 확인",
            dedup_key="recall_timeouts",
            window=window,
        ):
            _recall_timeouts.clear()
    except Exception:  # noqa: BLE001
        _log.warning("회상 타임아웃 경보 판정 실패", exc_info=True)


def slow_turn(total_ms: Any, **parts: Any) -> None:
    """대화 턴 총 소요가 기준을 넘으면 경보 — 구간별 소요를 같이 싣는다."""
    try:
        limit = settings.alert_slow_turn_ms
        if limit <= 0 or not isinstance(total_ms, (int, float)) or total_ms <= limit:
            return
        detail = " · ".join(
            f"{name} {value:.0f}ms" for name, value in parts.items() if isinstance(value, (int, float))
        )
        fire(
            f"🐢 대화 턴 {total_ms / 1000:.1f}초(기준 {limit / 1000:.0f}초)" + (f" — {detail}" if detail else ""),
            dedup_key="slow_turn",
            window=settings.alert_slow_turn_dedup_s,
        )
    except Exception:  # noqa: BLE001
        _log.warning("느린 턴 경보 판정 실패", exc_info=True)
