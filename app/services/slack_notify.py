"""슬랙 Incoming Webhook — 운영 알림(워커 요약·유저 피드백·모니터링 경보). URL 미설정 시 no-op.

severity 라우팅: alert=즉시 크리티컬(#moly-alerts) / status=상태·요약(#moly-status)
/ feedback=인앱 문의(#moly-feedback). 피드백 웹훅 미설정 시 alert → 공용 웹훅으로
폴백하고, 나머지 채널은 미설정 시 공용 slack_webhook_url로 폴백한다. dedup_key로 스톰·flapping 스팸 억제
(프로세스 내 best-effort — API·워커는 별도 프로세스라 각자 억제).
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

_log = logging.getLogger("moly-worker")

# 알림 dedup — key → 마지막 전송 monotonic 시각. asyncio 단일스레드라 check-and-set은 await 전 원자적.
_last_sent: dict[str, float] = {}


# HTTPX INFO는 webhook URL을, SDK DEBUG는 원문 body를 기록할 수 있다.
# 이 작업의 외부 전송 동안만 가리고 다른 동시 요청의 로그 설정은 변경하지 않는다.
_private_transport = ContextVar("private_notification_transport", default=False)


class _PrivateTransportFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _private_transport.get()


for _logger_name in (
    "httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy",
    "httpcore.socks", "openai", "openai._base_client", "openai._response", "openai._legacy_response",
):
    logging.getLogger(_logger_name).addFilter(_PrivateTransportFilter())


@contextmanager
def private_transport():
    token = _private_transport.set(True)
    try:
        yield
    finally:
        _private_transport.reset(token)


def _webhook_for(severity: str) -> str:
    """severity → 채널 웹훅. 채널 미설정 시 공용으로 폴백."""
    if severity == "alert":
        return settings.slack_alert_webhook_url or settings.slack_webhook_url
    if severity == "status":
        return settings.slack_status_webhook_url or settings.slack_webhook_url
    if severity == "feedback":
        return (
            settings.slack_feedback_webhook_url
            or settings.slack_alert_webhook_url
            or settings.slack_webhook_url
        )
    return settings.slack_webhook_url


async def _post(url: str, text: str, *, plain: bool = False) -> bool:
    """웹훅 1건 전송. URL 없으면 스킵. 오류는 로깅만(호출측 미중단)."""
    if not url:
        return False
    try:
        payload: dict = {"text": text}
        if plain:
            payload.update(mrkdwn=False, link_names=False, unfurl_links=False, unfurl_media=False)
        with private_transport():
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json=payload)
        if r.status_code != 200:
            _log.warning("슬랙 웹훅 응답 비정상 HTTP %s", r.status_code)
            return False
        return True
    except Exception as e:  # noqa: BLE001
        _log.warning("슬랙 웹훅 전송 실패: %s", type(e).__name__)
        return False


def claim(dedup_key: str, window: float | None = None) -> bool:
    """억제 창(기본 alert_dedup_window_sec) 안에 같은 키를 이미 보냈으면 False, 아니면 시각을 기록하고 True.

    await 없이 끝나므로 asyncio 안에서 원자적이다 — 전송 전에 억제를 정해야 하는 호출측(app 경보)도 쓴다.
    """
    now = time.monotonic()
    last = _last_sent.get(dedup_key)
    window = settings.alert_dedup_window_sec if window is None else window
    if last is not None and now - last < window:
        return False  # 창 내 중복 — 스팸 억제
    _last_sent[dedup_key] = now
    return True


async def send(text: str, *, severity: str = "status", dedup_key: str | None = None) -> bool:
    """기존 claim 계약을 유지하고, 전송 실패는 다음 시도에서 다시 관측한다."""
    if dedup_key and not claim(dedup_key):
        return False
    claimed_at = _last_sent.get(dedup_key) if dedup_key else None
    sent = await _post(_webhook_for(severity), text)
    if not sent and dedup_key and _last_sent.get(dedup_key) == claimed_at:
        _last_sent.pop(dedup_key, None)
    return sent


async def alert(text: str, *, dedup_key: str | None = None) -> bool:
    """즉시 크리티컬(#moly-alerts). 스톰 방지 위해 dedup_key 권장."""
    return await send(text, severity="alert", dedup_key=dedup_key)


async def send_feedback(text: str) -> bool:
    """인앱 피드백 전용 채널로 전송.

    전용 웹훅이 없는 기존 배포는 alert → 공용 웹훅을 그대로 사용한다.
    """
    return await _post(_webhook_for("feedback"), text, plain=True)


def escape(text: str) -> str:
    """Slack 제어 문자를 문자 그대로 표시한다(mention/link 삽입 방지)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def feedback_text(
    user_id: str,
    nickname: str | None,
    message: str,
    contact: str | None,
    when: datetime | None = None,
    *,
    translation: str | None = None,
    translation_unavailable: bool = False,
) -> str:
    """유저 인앱 피드백 슬랙 메시지 포맷(내부 채널).

    연락처는 있을 때만 표시(의견만 남기면 내용뿐), 유저는 닉네임+id, 시간은 한국 시각.
    """
    when = when or datetime.now(ZoneInfo("Asia/Seoul"))
    lines = ["🗣️ 새 피드백 도착", f"원문: {escape(message)}"]
    if translation:
        lines.append(f"한국어 (자동 번역): {escape(translation)}")
    elif translation_unavailable:
        lines.append("한국어 번역 미제공 — 원문을 확인해 주세요.")
    if contact:
        lines.append(f"연락처: {escape(contact)}")
    lines.append(f"유저: {escape(nickname or '(닉네임 없음)')} ({escape(user_id)})")
    lines.append(f"시간: {when:%Y-%m-%d %H:%M}")
    return "\n".join(lines)


async def send_summary(text: str) -> None:
    """워커 요약 등 상태성 메시지 → status 채널(조용). 하위호환 유지."""
    await _post(_webhook_for("status"), text)
