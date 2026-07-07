"""FCM(Firebase Cloud Messaging) 발송 — 클라가 FCM SDK로 받은 토큰으로 푸시.

APNs .p8는 Firebase 콘솔에 업로드됨 → Firebase가 APNs로 릴레이. 백엔드는 FCM HTTP v1로 발송.
자격증명(service account) 미설정 시 no-op(로그만) — 팀원 제공 후 실발송.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

_log = logging.getLogger("moly-worker")
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def _access_token() -> str | None:
    if not (settings.fcm_service_account_file and settings.fcm_project_id):
        return None
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        settings.fcm_service_account_file, scopes=[_SCOPE]
    )
    creds.refresh(Request())
    return creds.token


async def send(tokens: list[str], title: str, body: str) -> int:
    """토큰들에 알림 발송, 성공 건수 반환. 미설정/토큰없음이면 0(no-op)."""
    if not tokens:
        return 0
    token = _access_token()
    if token is None:
        _log.info("FCM 미설정 — 발송 스킵(대상 %d)", len(tokens))
        return 0
    url = f"https://fcm.googleapis.com/v1/projects/{settings.fcm_project_id}/messages:send"
    sent = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        for t in tokens:
            msg = {"message": {"token": t, "notification": {"title": title, "body": body}}}
            r = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=msg)
            if r.status_code == 200:
                sent += 1
            else:
                _log.info("FCM 발송 실패 HTTP %s", r.status_code)
    return sent
