"""AdMob 리워드 SSV 서명검증 — Google verifier 공개키(ECDSA P-256)로 검증.

서명 대상 = 콜백 쿼리스트링에서 '&signature=' 이전 전체(원본 순서). 키는 key_id로 매칭.
클라는 서명을 다루지 않음 — 시청 확정은 반드시 서버-서버 SSV로(ERD §4.2).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_public_key

_log = logging.getLogger("moly-backend")
_KEYS_URL = "https://www.gstatic.com/admob/reward/verifier-keys.json"
_KEYS_TTL_SECONDS = 24 * 60 * 60  # Google 정책: 공개키 24시간 이상 캐시 금지(수시 로테이션)
_FORCE_MIN_INTERVAL = 60.0  # 미등록 key_id 강제 재조회 최소 간격(초) — 서명 없는 refetch 폭주(DoS) 차단
_keys_cache: dict[str, str] | None = None
_keys_fetched_at: float = 0.0
_last_force_at: float = 0.0
_keys_lock = asyncio.Lock()


async def _get_keys(*, force: bool = False) -> dict[str, str]:
    """Google verifier 공개키 캐시(TTL 24h). force = 미등록 key_id 재조회이나 최소간격 스로틀·락으로
    동시 콜드스타트와 refetch 폭주를 막는다 — 공개 SSV 엔드포인트 DoS 방어(SOMA-376)."""
    global _keys_cache, _keys_fetched_at, _last_force_at
    now = time.monotonic()
    if _keys_cache is not None and now - _keys_fetched_at < _KEYS_TTL_SECONDS and not force:
        return _keys_cache  # 빠른 경로: 정상 캐시 히트(락 없음)
    async with _keys_lock:  # 동시 갱신 직렬화(중복 외부요청 방지)
        now = time.monotonic()
        expired = now - _keys_fetched_at >= _KEYS_TTL_SECONDS
        do_force = force and (now - _last_force_at >= _FORCE_MIN_INTERVAL)  # 강제 재조회 스로틀
        if _keys_cache is not None and not expired and not do_force:
            return _keys_cache  # 락 대기 중 갱신됐거나, 강제 스로틀에 걸림
        async with httpx.AsyncClient(timeout=10.0) as client:
            data = (await client.get(_KEYS_URL)).json()
        _keys_cache = {str(k["keyId"]): k["pem"] for k in data.get("keys", [])}
        _keys_fetched_at = time.monotonic()
        if do_force:
            _last_force_at = _keys_fetched_at
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
        if not pem:  # 캐시에 없는 key_id → Google 키 로테이션 대응 재조회
            pem = (await _get_keys(force=True)).get(str(key_id))
        if not pem:
            return False
        public_key = load_pem_public_key(pem.encode())
        signature = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
        public_key.verify(signature, content, ec.ECDSA(hashes.SHA256()))  # DER 서명
        return True
    except Exception as e:  # noqa: BLE001  # 검증 실패는 조용히 거절
        _log.info("AdMob SSV 검증 실패: %r", e)
        return False
