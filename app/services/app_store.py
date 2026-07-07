"""App Store(StoreKit 2) JWS 디코드 — 결제/알림 페이로드 추출.

⚠️ MVP: **서명검증 없이 payload 디코드**. 프로덕션은 Apple 인증서체인(x5c) 검증 필수
(app-store-server-library 등) — App Store Server API 키 확보 후 교체. 지금은 mock/구조용.
"""
from __future__ import annotations

import logging

import jwt

from app.core import errors

_log = logging.getLogger("moly-backend")


def decode(signed: str) -> dict:
    try:
        return jwt.decode(signed, options={"verify_signature": False, "verify_aud": False})
    except Exception as e:  # noqa: BLE001
        _log.info("StoreKit JWS 디코드 실패: %r", e)
        raise errors.receipt_invalid() from e
