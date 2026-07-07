"""AdMob 리워드 SSV 서명검증 — Google verifier 공개키(ECDSA P-256)로 검증.

서명 대상 = 콜백 쿼리스트링에서 '&signature=' 이전 전체(원본 순서). 키는 key_id로 매칭.
클라는 서명을 다루지 않음 — 시청 확정은 반드시 서버-서버 SSV로(ERD §4.2).
"""
from __future__ import annotations

import base64
import logging

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_public_key

_log = logging.getLogger("moly-backend")
_KEYS_URL = "https://www.gstatic.com/admob/reward/verifier-keys.json"
_keys_cache: dict[str, str] | None = None


async def _get_keys() -> dict[str, str]:
    global _keys_cache
    if _keys_cache is None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            data = (await client.get(_KEYS_URL)).json()
        _keys_cache = {str(k["keyId"]): k["pem"] for k in data.get("keys", [])}
    return _keys_cache


def _signed_content(raw_query: str) -> bytes | None:
    idx = raw_query.find("&signature=")
    return raw_query[:idx].encode() if idx >= 0 else None


async def verify(raw_query: str, key_id: str, signature_b64: str) -> bool:
    """SSV 콜백 서명 검증. 실패/오류 = False(거절)."""
    content = _signed_content(raw_query)
    if content is None:
        return False
    try:
        pem = (await _get_keys()).get(str(key_id))
        if not pem:
            return False
        public_key = load_pem_public_key(pem.encode())
        signature = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
        public_key.verify(signature, content, ec.ECDSA(hashes.SHA256()))  # DER 서명
        return True
    except Exception as e:  # noqa: BLE001  # 검증 실패는 조용히 거절
        _log.info("AdMob SSV 검증 실패: %r", e)
        return False
