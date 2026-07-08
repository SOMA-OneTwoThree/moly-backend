"""FCM(Firebase Cloud Messaging) 발송 — 클라가 FCM SDK로 받은 토큰으로 푸시.

APNs .p8는 Firebase 콘솔에 업로드됨 → Firebase가 APNs로 릴레이. 백엔드는 FCM HTTP v1로 발송.

인증: **다운로드 키 파일 없이도 동작**(Google 권장 — 키 유출 위험 회피).
- `FCM_SERVICE_ACCOUNT_FILE` 지정 시: 그 키 파일 사용(레거시/명시 오버라이드).
- 미지정 시: **ADC**(google.auth.default) 자동 발견 —
  · GCP 배포(Cloud Run/GCE): 컴퓨트에 붙인 서비스 계정
  · 비-GCP 배포: Workload Identity Federation 설정(GOOGLE_APPLICATION_CREDENTIALS=config)
  · 로컬: `gcloud auth application-default login`
- 아무 자격증명도 없으면 no-op(로그만).
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

_log = logging.getLogger("moly-worker")
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def _access_token() -> str | None:
    if not settings.fcm_project_id:
        return None
    from google.auth.transport.requests import Request

    if settings.fcm_service_account_file:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            settings.fcm_service_account_file, scopes=[_SCOPE]
        )
    else:
        # 키리스: 배포 환경의 ADC(연결 SA / WIF / 로컬 gcloud) 자동 발견.
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        try:
            creds, _ = google.auth.default(scopes=[_SCOPE])
        except DefaultCredentialsError:
            return None
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
