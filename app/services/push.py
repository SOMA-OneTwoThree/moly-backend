"""FCM(Firebase Cloud Messaging) 발송 — 클라가 FCM SDK로 받은 토큰으로 푸시.

APNs .p8는 Firebase 콘솔에 업로드됨 → Firebase가 APNs로 릴레이. 백엔드는 FCM HTTP v1로 발송.

인증: **다운로드 키 파일 없이도 동작**(Google 권장 — 키 유출 위험 회피).
- `FCM_SERVICE_ACCOUNT_FILE` 지정 시: 그 키 파일 사용(레거시/명시 오버라이드).
- 미지정 시: **ADC**(google.auth.default) 자동 발견 —
  · GCP 배포(Cloud Run/GCE): 컴퓨트에 붙인 서비스 계정
  · 비-GCP 배포: Workload Identity Federation 설정(GOOGLE_APPLICATION_CREDENTIALS=config)
  · 로컬: `gcloud auth application-default login`
- 아무 자격증명도 없으면 no-op(로그만).

실패 분류: HTTP 상태만으로 판단하지 않고 **응답 본문의 FcmError.errorCode**로 판정한다.
404는 프로젝트 경로 오타(NOT_FOUND, FcmError 없음)로도 나오고, 400은 페이로드 오류로도
나오며, 403은 서비스 계정 권한 부족(전 토큰 공통)으로도 나온다. 토큰 자체가 무효라고
FCM이 확정한 경우(UNREGISTERED · 토큰 필드 INVALID_ARGUMENT · SENDER_ID_MISMATCH)만 무효로
분류하고, 그중 `settings.fcm_invalidate_codes`(기본 UNREGISTERED만)에 든 코드만
`SendResult.invalid_tokens`에 실어 호출자가 user_devices에 비활성 표시를 할 수 있게 한다.
(공식 문서: INVALID_ARGUMENT 400 / UNREGISTERED 404 / SENDER_ID_MISMATCH 403 /
 THIRD_PARTY_AUTH_ERROR 401(APNs 키) / QUOTA_EXCEEDED 429 / UNAVAILABLE 503 / INTERNAL 500.)
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from datetime import datetime, timezone

import httpx

from app.config import settings

_log = logging.getLogger("moly-worker")
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# 프로세스 수명 자격증명 캐시 — 액세스 토큰(수명 1h)을 만료 전까지 재사용한다. 종전엔 유저마다 자격증명을
# 새로 만들고 refresh(토큰 엔드포인트 왕복)해서 푸시 틱마다 발송 유저 수만큼 반복됐다.
# _access_token은 to_thread로 돌므로 동시성>1이면 스레드가 겹친다 → 락으로 refresh를 1회로 합류.
_creds = None
_creds_lock = threading.Lock()


def _load_credentials():
    """자격증명 객체 생성(네트워크 없음). 아무 자격증명도 없으면 None(no-op)."""
    if settings.fcm_service_account_file:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            settings.fcm_service_account_file, scopes=[_SCOPE]
        )
    # 키리스: 배포 환경의 ADC(연결 SA / WIF / 로컬 gcloud) 자동 발견.
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    try:
        creds, _ = google.auth.default(scopes=[_SCOPE])
    except DefaultCredentialsError:
        return None
    return creds


def _refresh_request():
    from google.auth.transport.requests import Request

    return Request()

_FCM_ERROR_TYPE = "type.googleapis.com/google.firebase.fcm.v1.FcmError"
_BAD_REQUEST_TYPE = "type.googleapis.com/google.rpc.BadRequest"

# 실패 분류 키(관측 카운트 키와 1:1 — tick의 counts 초기화가 이 이름을 쓴다).
INVALID_TOKEN = "invalid_token"  # 토큰 자체가 죽음 → user_devices에서 지워도 안전
PAYLOAD_ERROR = "payload_error"  # 400인데 토큰 필드가 아님 → 우리 메시지 버그, 토큰 유지
SETUP_ERROR = "setup_error"      # 401 APNs 키·403 권한·404 프로젝트 — 전 토큰 공통 장애, 토큰 유지
TRANSIENT = "transient"          # 429·5xx·네트워크 — 다음 틱에 다시
FAILURE_KINDS: tuple[str, ...] = (INVALID_TOKEN, PAYLOAD_ERROR, SETUP_ERROR, TRANSIENT)

# 오류 메시지 요약에서 토큰처럼 생긴 긴 문자열은 가린다(FCM 본문엔 토큰이 없지만 방어).
_LONG_OPAQUE = re.compile(r"[A-Za-z0-9_\-:]{40,}")


class SendResult(int):
    """`send()`의 반환값 — int(FCM이 수락한 건수)와 완전 호환(비교·truthiness·합산).

    기존 호출자와 테스트의 int fake가 그대로 동작하도록 int를 상속하고, 토큰 비활성 표시·관측에
    필요한 분류만 속성으로 얹는다. `invalid_tokens` = FCM이 무효라고 확정했고 설정의 허용
    코드에 든 토큰, `failures` = 분류별 실패 건수(FAILURE_KINDS 키).
    """

    invalid_tokens: tuple[str, ...]
    failures: dict[str, int]

    def __new__(cls, sent: int = 0, invalid_tokens=(), failures: dict[str, int] | None = None):
        obj = super().__new__(cls, sent)
        obj.invalid_tokens = tuple(invalid_tokens)
        obj.failures = dict(failures or {})
        return obj


def classify_failure(status: int, body: object) -> tuple[str, str]:
    """(분류, FcmError.errorCode). body = 응답 JSON(없으면 None).

    토큰 삭제는 INVALID_TOKEN일 때만 안전하다. 400은 BadRequest.fieldViolations가 토큰 필드를
    가리킬 때만 토큰 문제로 본다(페이로드 오류와 구분 — Firebase 문서: "메시지 페이로드가
    유효하다고 확신할 때만" 400/404에 토큰을 지우라고 한다).
    """
    err = body.get("error") if isinstance(body, dict) else None
    details = err.get("details") if isinstance(err, dict) else None
    code = ""
    token_field = False
    for d in details or ():
        if not isinstance(d, dict):
            continue
        kind = d.get("@type")
        if kind == _FCM_ERROR_TYPE:
            code = str(d.get("errorCode") or "")
        elif kind == _BAD_REQUEST_TYPE:
            for v in d.get("fieldViolations") or ():
                if isinstance(v, dict) and str(v.get("field") or "").endswith("token"):
                    token_field = True
    if status == 404 and code == "UNREGISTERED":
        return INVALID_TOKEN, code
    if status == 400 and code == "INVALID_ARGUMENT" and token_field:
        return INVALID_TOKEN, code
    if status == 403 and code == "SENDER_ID_MISMATCH":
        return INVALID_TOKEN, code
    if status == 400:
        return PAYLOAD_ERROR, code
    if status in (401, 403, 404):
        return SETUP_ERROR, code
    return TRANSIENT, code


def _brief(body: object) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    msg = str(err.get("message") or "") if isinstance(err, dict) else ""
    return _LONG_OPAQUE.sub("<opaque>", msg)[:160]


def invalidate_codes() -> frozenset[str]:
    """비활성 표시 대상 errorCode 집합(설정, 쉼표 구분, 대소문자 무시)."""
    return frozenset(
        c.strip().upper() for c in settings.fcm_invalidate_codes.split(",") if c.strip()
    )


def _access_token() -> str | None:
    global _creds
    if not settings.fcm_project_id:
        return None
    from google.auth.credentials import TokenState

    with _creds_lock:
        if _creds is None:
            _creds = _load_credentials()
            if _creds is None:
                return None  # 자격증명 없음은 캐시하지 않는다(다음 호출에서 다시 찾음)
        # 미발급·만료·만료 임박(라이브러리 기준 3분 45초 전)일 때만 토큰 엔드포인트 왕복.
        if _creds.token_state != TokenState.FRESH:
            _creds.refresh(_refresh_request())
        return _creds.token


async def prepare_access_token() -> str | None:
    # Credential refresh can perform blocking network I/O.
    return await asyncio.to_thread(_access_token)


async def send(
    tokens: list[str], title: str, body: str, *, data: dict[str, str] | None = None,
    expires_at: datetime | None = None, access_token: str | None = None,
) -> SendResult:
    """토큰들에 알림 발송, 성공 건수(SendResult, int 호환) 반환. 미설정/토큰없음이면 0(no-op)."""
    if not tokens:
        return SendResult(0)
    token = access_token or await prepare_access_token()
    if token is None:
        _log.info("FCM 미설정 — 발송 스킵(대상 %d)", len(tokens))
        return SendResult(0)
    url = f"https://fcm.googleapis.com/v1/projects/{settings.fcm_project_id}/messages:send"
    sent = 0
    invalid: list[str] = []
    failures: dict[str, int] = {}
    allow = invalidate_codes()
    async with httpx.AsyncClient(timeout=10.0) as client:
        for t in tokens:
            msg = {"message": {"token": t, "notification": {"title": title, "body": body}}}
            if data:
                msg["message"]["data"] = data
            if expires_at is not None:
                ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds())
                if ttl <= 0:
                    break
                msg["message"]["android"] = {"ttl": f"{ttl}s"}
                msg["message"]["apns"] = {
                    "headers": {"apns-expiration": str(int(expires_at.timestamp()))},
                }
            try:
                r = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=msg)
            except httpx.RequestError:
                _log.warning("FCM 네트워크 오류 — 해당 기기 재시도 생략")
                failures[TRANSIENT] = failures.get(TRANSIENT, 0) + 1
                continue
            if r.status_code == 200:
                sent += 1
                continue
            try:
                payload = r.json()
            except ValueError:
                payload = None
            kind, code = classify_failure(r.status_code, payload)
            failures[kind] = failures.get(kind, 0) + 1
            if kind == INVALID_TOKEN:
                # 허용 코드만 비활성 대상. 나머지(예: 기본 설정의 SENDER_ID_MISMATCH)는 분류·집계만.
                if code in allow:
                    invalid.append(t)
                _log.info(
                    "FCM 발송 실패 HTTP %s %s — 무효 토큰(%s)",
                    r.status_code, code, "비활성 대상" if code in allow else "표시 안 함",
                )
            else:
                # 토큰 문제가 아닌 실패는 우리 쪽 원인(페이로드·자격증명·FCM 장애)일 수 있어 본문 요지를 남긴다.
                _log.warning(
                    "FCM 발송 실패 HTTP %s %s [%s] — 토큰 유지: %s",
                    r.status_code, code or "-", kind, _brief(payload),
                )
    return SendResult(sent, invalid, failures)
