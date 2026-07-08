"""App Store(StoreKit 2) JWS 검증·디코드 — 구독/IAP 영수증 + ASSN 웹훅.

x5c 인증서체인을 **Apple Root CA - G3**로 검증(app-store-server-library).
우리 설계는 App Store Server API 조회가 없어 .p8/Key ID/Issuer ID 불필요 — 검증만 한다.

검증 성공 후 **Apple 원본 JSON(camelCase) dict를 반환** — 호출부는 payload.get("productId") 등
기존 인터페이스를 그대로 사용(라이브러리 typed 객체로 갈아끼우지 않음, 필드 드리프트 방지).

설정(app_store_bundle_id) 있으면 로컬·프로덕션 모두 실제 서명검증.
미설정 시: 로컬은 미검증 디코드(개발 편의), 비로컬은 fail-closed(위조 영수증/웹훅 거부).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import jwt

from app.config import settings
from app.core import errors

_log = logging.getLogger("moly-backend")

_ROOT_CA = Path(__file__).parent / "apple_certs" / "AppleRootCA-G3.cer"
# OCSP 온라인 실효성검사 비활성 — 체인+루트핀+서명 검증으로 신뢰 확보. 요청당 Apple OCSP
# 네트워크 의존 제거(지연·플래키 방지). 필요 시 True로 전환.
_ONLINE_CHECKS = False


@lru_cache
def _verifier():
    """SignedDataVerifier 싱글턴. bundle_id 미설정이면 None(미검증 폴백 판단용)."""
    if not settings.app_store_bundle_id:
        return None
    from appstoreserverlibrary.models.Environment import Environment
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

    env = (
        Environment.PRODUCTION
        if settings.app_store_environment.lower() == "production"
        else Environment.SANDBOX
    )
    root = _ROOT_CA.read_bytes()
    return SignedDataVerifier(
        [root], _ONLINE_CHECKS, env, settings.app_store_bundle_id,
        settings.app_store_app_apple_id,
    )


def _raw_payload(signed: str) -> dict:
    """검증 후 원본 JSON dict 추출(서명은 라이브러리가 이미 검증). camelCase·중첩 그대로."""
    try:
        return jwt.decode(signed, options={"verify_signature": False, "verify_aud": False})
    except Exception as e:  # noqa: BLE001
        _log.info("StoreKit JWS 디코드 실패: %r", e)
        raise errors.receipt_invalid() from e


def _fallback_or_reject(signed: str) -> dict:
    """검증기 미설정 시: 로컬만 미검증 디코드 허용, 비로컬은 fail-closed."""
    if settings.environment != "local":
        _log.error("StoreKit 검증기 미설정(app_store_bundle_id) — 비로컬 결제/웹훅 거부(fail-closed)")
        raise errors.receipt_invalid()
    return _raw_payload(signed)


def decode_transaction(signed_transaction: str) -> dict:
    """서명된 거래(JWSTransaction) 검증 → 원본 dict. 구독 verify/restore·IAP·웹훅 tx_info용."""
    v = _verifier()
    if v is None:
        return _fallback_or_reject(signed_transaction)
    from appstoreserverlibrary.signed_data_verifier import VerificationException
    try:
        v.verify_and_decode_signed_transaction(signed_transaction)  # 신뢰판단(실패 시 raise)
    except VerificationException as e:
        _log.warning("StoreKit 거래 서명검증 실패: %r", e)
        raise errors.receipt_invalid() from e
    return _raw_payload(signed_transaction)


def decode_notification(signed_payload: str) -> dict:
    """ASSN v2 알림(ResponseBodyV2) 검증 → 원본 dict. data.signedTransactionInfo는 별도 검증."""
    v = _verifier()
    if v is None:
        return _fallback_or_reject(signed_payload)
    from appstoreserverlibrary.signed_data_verifier import VerificationException
    try:
        v.verify_and_decode_notification(signed_payload)  # 신뢰판단(실패 시 raise)
    except VerificationException as e:
        _log.warning("StoreKit 알림 서명검증 실패: %r", e)
        raise errors.receipt_invalid() from e
    return _raw_payload(signed_payload)
