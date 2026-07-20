"""슬랙 Incoming Webhook — 워커 일일 요약 전송. URL 미설정 시 no-op."""
from __future__ import annotations

import logging

import httpx

from app.config import settings

_log = logging.getLogger("moly-worker")


async def send_summary(text: str) -> None:
    """슬랙에 텍스트 메시지 1건 전송. URL 없으면 조용히 스킵. 오류는 로깅만(배치 미중단)."""
    if not settings.slack_webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(settings.slack_webhook_url, json={"text": text})
        if r.status_code != 200:
            _log.warning("슬랙 웹훅 응답 비정상 HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        _log.warning("슬랙 웹훅 전송 실패: %r", e)
