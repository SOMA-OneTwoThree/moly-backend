"""슬랙 Incoming Webhook — 운영 알림(워커 일일 요약·유저 피드백). URL 미설정 시 no-op."""
from __future__ import annotations

import logging

import httpx

from app.config import settings

_log = logging.getLogger("moly-worker")


def feedback_text(user_id: str, message: str, contact: str | None) -> str:
    """유저 인앱 피드백 슬랙 메시지 포맷(내부 채널)."""
    return (
        "🗣️ 새 피드백 도착\n"
        f"내용: {message}\n"
        f"연락처: {contact or '없음'}\n"
        f"유저: {user_id}"
    )


async def send_summary(text: str) -> None:
    """슬랙에 텍스트 메시지 1건 전송(요약·알림 공용). URL 없으면 조용히 스킵. 오류는 로깅만(미중단)."""
    if not settings.slack_webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(settings.slack_webhook_url, json={"text": text})
        if r.status_code != 200:
            _log.warning("슬랙 웹훅 응답 비정상 HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        _log.warning("슬랙 웹훅 전송 실패: %r", e)
