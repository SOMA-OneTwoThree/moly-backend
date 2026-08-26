"""Meta 설치 귀속 — Google Play 설치 리퍼러의 utm_content(AES-256-GCM 암호문)를 광고 귀속으로 복호화.

앱이 설치 직후(로그인 전) 부르는 경로라 인증이 없다. 서버는 복호화 키만 쥐고 상태를 만들지 않는다.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings
from app.core import errors

# Meta가 실어 보내는 귀속 필드. 숫자처럼 보이는 ID도 전부 JSON 문자열로 온다.
ATTRIBUTION_FIELDS = (
    "ad_id",
    "adgroup_id",
    "adgroup_name",
    "campaign_id",
    "campaign_name",
    "campaign_group_id",
    "campaign_group_name",
    "account_id",
    "ad_objective_name",
)


def _as_object(raw: str) -> dict[str, Any] | None:
    """utm_content는 URL 디코드된 JSON일 수도, 아직 percent-encoded 상태일 수도 있다."""
    for candidate in (raw, unquote(raw)):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _ciphertext(raw: str) -> tuple[bytes, bytes] | None:
    """(nonce, data). Meta 암호문이 없거나 hex가 깨졌으면 None — 정상적인 '귀속 없음'이다."""
    parsed = _as_object(raw)
    source = parsed.get("source") if parsed is not None else None
    if not isinstance(source, dict):
        return None
    data, nonce = source.get("data"), source.get("nonce")
    if not (isinstance(data, str) and isinstance(nonce, str) and data and nonce):
        return None
    try:
        return bytes.fromhex(nonce), bytes.fromhex(data)
    except ValueError:
        return None


def _key() -> bytes:
    """설정된 64자 hex 키. 미설정·형식 오류는 서버 설정 문제라 503(클라는 재시도 가능)."""
    try:
        key = bytes.fromhex(settings.meta_install_referrer_decryption_key)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise errors.attribution_key_unavailable()
    return key


def decrypt_meta_referrer(utm_content: str) -> dict[str, str] | None:
    """복호화된 귀속 필드. 암호문이 아예 없으면 None(오류 아님)."""
    parts = _ciphertext(utm_content)
    if parts is None:
        return None
    nonce, data = parts
    key = _key()
    try:
        # 16바이트 인증 태그는 암호문 뒤에 붙어 있고, AESGCM.decrypt가 직접 떼어내 검증한다.
        # Meta 공식 문서는 "평문 끝 16바이트를 잘라내라"고 하지만 AEAD API에는 틀린 지시다 —
        # 여기서 자르면 멀쩡한 JSON이 깨진다. 자르지 마라.
        payload = json.loads(AESGCM(key).decrypt(nonce, data, None))
    except (InvalidTag, ValueError) as exc:
        raise errors.attribution_decrypt_failed() from exc
    if not isinstance(payload, dict):
        raise errors.attribution_decrypt_failed()
    return {
        field: str(payload[field])
        for field in ATTRIBUTION_FIELDS
        if payload.get(field) is not None
    }
