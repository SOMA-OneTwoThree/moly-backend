"""워커 합성 점검 — 틱마다 공개 주소로 `/health/synthetic`을 한 번 부른다(실패면 5초 뒤 한 번 더).

공개 주소를 부르므로 ALB·nginx·API·DB·LLM 전 구간을 본다. 결과는 경보로만 보낸다 — 데드맨(/fail)은
워커 자신의 결과만 판정한다(API 장애를 워커 실패로 섞지 않는다). 틱 본 작업과 동시에 돌고 상한이 있어
틱을 오래 붙잡지 않는다. 워커는 틱마다 새 프로세스라 장애가 이어지면 틱마다 한 번씩 경보한다.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings
from app.services import slack_notify

_log = logging.getLogger("moly-worker")

# 배포 스모크·GitHub 합성 모니터와 같은 운영 공개 주소.
PRODUCTION_SYNTHETIC_URL = "https://voice.moly.asia/health/synthetic"
_RETRY_DELAY_S = 5.0
# 거둘 때 기다리는 상한의 여유 — 두 번의 시도와 재시도 대기에 더한다.
_CAP_SLACK_S = 5.0


def target_url() -> str:
    """부를 주소. 비면 점검하지 않는다."""
    if not settings.worker_synthetic_enabled:
        return ""
    if settings.worker_synthetic_url:
        return settings.worker_synthetic_url
    return PRODUCTION_SYNTHETIC_URL if settings.environment == "production" else ""


def start() -> asyncio.Task | None:
    """점검을 태스크로 띄운다(틱과 동시에). 주소·토큰이 없으면 None."""
    try:
        url = target_url()
        if not url or not settings.health_token:
            return None
        return asyncio.get_running_loop().create_task(_probe(url))
    except Exception:  # noqa: BLE001  # 점검 준비 실패가 틱을 막으면 안 된다
        _log.warning("합성 점검 시작 실패(무시)", exc_info=True)
        return None


def _latency(r: httpx.Response) -> str:
    try:
        ms = r.json()["llm"]["latency_ms"]
        return f" (llm {int(ms)}ms)"
    except Exception:  # noqa: BLE001
        return ""


async def _probe(url: str) -> str | None:
    """정상이면 None, 두 번 다 실패면 마지막 사유(예: "HTTP 503", "ConnectTimeout")."""
    reason: str | None = None
    async with httpx.AsyncClient(timeout=settings.worker_synthetic_timeout_s) as client:
        for attempt in range(2):
            try:
                r = await client.get(url, headers={"X-Health-Token": settings.health_token})
            except httpx.HTTPError as e:
                reason = type(e).__name__
            else:
                if r.status_code == 200:
                    _log.info("합성 점검 200%s", _latency(r))
                    return None
                reason = f"HTTP {r.status_code}"
            if attempt == 0:
                await asyncio.sleep(_RETRY_DELAY_S)
    return reason


async def finish(task: asyncio.Task | None) -> None:
    """점검을 거둔다(상한 대기). 실패면 경보. 어떤 실패도 밖으로 던지지 않는다.

    문구는 실제로 일어난 일만 말한다 — 재시도까지 실패 / 상한 안에 끝나지 않음 / 점검 자체 오류.
    """
    if task is None:
        return
    cap = 2 * settings.worker_synthetic_timeout_s + _RETRY_DELAY_S + _CAP_SLACK_S
    try:
        reason = await asyncio.wait_for(task, timeout=cap)
    except TimeoutError:  # 상한 초과 — 재시도까지 갔는지는 모른다
        text = f"🔴 합성 점검 실패(워커) — /health/synthetic {cap:.0f}초 안에 응답 없음. API·DB·LLM 확인"
    except Exception as e:  # noqa: BLE001  # 점검 코드·설정 오류 — 재시도 없이 끝났다
        text = f"⚠️ 합성 점검 자체 오류(워커) — {type(e).__name__}. 점검 설정·워커 로그 확인"
    else:
        if reason is None:
            return
        text = (
            f"🔴 합성 점검 실패(워커) — /health/synthetic {reason}, {_RETRY_DELAY_S:.0f}초 뒤 재시도도 실패."
            " API·DB·LLM 확인"
        )
    _log.warning("합성 점검 경보: %s", text)
    try:
        await slack_notify.alert(text)
    except Exception:  # noqa: BLE001
        _log.warning("합성 점검 경보 실패(무시)", exc_info=True)
