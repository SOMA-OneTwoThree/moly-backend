"""슬랙 Incoming Webhook — 운영 알림(워커 요약·유저 피드백·모니터링 경보). URL 미설정 시 no-op.

severity 라우팅: alert=즉시 크리티컬(#moly-alerts) / status=상태·요약(#moly-status).
채널별 웹훅 미설정 시 공용 slack_webhook_url로 폴백. dedup_key로 스톰·flapping 스팸 억제
(프로세스 내 best-effort — API·워커는 별도 프로세스라 각자 억제).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

_log = logging.getLogger("moly-worker")

# 알림 dedup — key → 마지막 전송 monotonic 시각. asyncio 단일스레드라 check-and-set은 await 전 원자적.
_last_sent: dict[str, float] = {}


def _webhook_for(severity: str) -> str:
    """severity → 채널 웹훅. 채널 미설정 시 공용으로 폴백."""
    if severity == "alert":
        return settings.slack_alert_webhook_url or settings.slack_webhook_url
    if severity == "status":
        return settings.slack_status_webhook_url or settings.slack_webhook_url
    return settings.slack_webhook_url


async def _post(url: str, text: str) -> None:
    """웹훅 1건 전송. URL 없으면 스킵. 오류는 로깅만(호출측 미중단)."""
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json={"text": text})
        if r.status_code != 200:
            _log.warning("슬랙 웹훅 응답 비정상 HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        _log.warning("슬랙 웹훅 전송 실패: %r", e)


async def send(text: str, *, severity: str = "status", dedup_key: str | None = None) -> None:
    """severity 채널로 전송. dedup_key 지정 시 alert_dedup_window_sec 내 같은 키는 억제."""
    if dedup_key:
        now = time.monotonic()
        last = _last_sent.get(dedup_key)
        if last is not None and now - last < settings.alert_dedup_window_sec:
            return  # 창 내 중복 — 스팸 억제
        _last_sent[dedup_key] = now
    await _post(_webhook_for(severity), text)


async def alert(text: str, *, dedup_key: str | None = None) -> None:
    """즉시 크리티컬(#moly-alerts). 스톰 방지 위해 dedup_key 권장."""
    await send(text, severity="alert", dedup_key=dedup_key)


def feedback_text(
    user_id: str,
    nickname: str | None,
    message: str,
    contact: str | None,
    when: datetime | None = None,
) -> str:
    """유저 인앱 피드백 슬랙 메시지 포맷(내부 채널).

    연락처는 있을 때만 표시(의견만 남기면 내용뿐), 유저는 닉네임+id, 시간은 한국 시각.
    """
    when = when or datetime.now(ZoneInfo("Asia/Seoul"))
    lines = ["🗣️ 새 피드백 도착", f"내용: {message}"]
    if contact:
        lines.append(f"연락처: {contact}")
    lines.append(f"유저: {nickname or '(닉네임 없음)'} ({user_id})")
    lines.append(f"시간: {when:%Y-%m-%d %H:%M}")
    return "\n".join(lines)


async def send_summary(text: str) -> None:
    """워커 요약·피드백 등 상태성 메시지 → status 채널(조용). 하위호환 유지."""
    await _post(_webhook_for("status"), text)
