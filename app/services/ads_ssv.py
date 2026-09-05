"""AdMob 리워드 SSV 서명검증 — Google verifier 공개키(ECDSA P-256)로 검증.

서명 대상 = 콜백 쿼리스트링에서 '&signature=' 이전 전체(원본 순서). 키는 key_id로 매칭.
클라는 서명을 다루지 않음 — 시청 확정은 반드시 서버-서버 SSV로(ERD §4.2).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import parse_qsl

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

_MAX_QUERY_LENGTH = 8192
_KEY_ID_RE = re.compile(r"^[0-9]{1,20}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{64,512}$")
_BAD_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_MAX_FIELD_LENGTHS = {
    "ad_network": 64,
    "ad_unit": 128,
    "custom_data": 1024,
    "reward_amount": 32,
    "reward_item": 128,
    "timestamp": 32,
    "transaction_id": 256,
    "user_id": 256,
}

_CRITICAL_FIELDS = frozenset({
    "ad_network",
    "ad_unit",
    "custom_data",
    "reward_amount",
    "reward_item",
    "timestamp",
    "transaction_id",
    "user_id",
})


@dataclass(frozen=True)
class VerifiedSsvPayload:
    """ECDSA 검증을 통과한 signed prefix의 파라미터만 노출한다."""

    key_id: str
    parameters: Mapping[str, str]

    def get(self, name: str) -> str | None:
        return self.parameters.get(name)


@dataclass(frozen=True)
class _SsvEnvelope:
    signed_content: bytes
    signature: str
    key_id: str
    parameters: Mapping[str, str]


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
        if do_force:
            # 시도 시각을 fetch 전에 기록 — Google 키서버 장애(timeout/5xx)로 실패해도 스로틀이
            # 걸리게 한다(성공에만 기록하면 장애 중 미등록 key_id마다 10초 외부호출 폭주).
            _last_force_at = now
        async with httpx.AsyncClient(timeout=10.0) as client:
            data = (await client.get(_KEYS_URL)).json()
        _keys_cache = {str(k["keyId"]): k["pem"] for k in data.get("keys", [])}
        _keys_fetched_at = time.monotonic()
    return _keys_cache


def _parse_envelope(raw_query: str) -> _SsvEnvelope | None:
    """Google의 고정 envelope를 검증하고 signed prefix만 파싱한다.

    정상 SSV는 마지막 두 query가 정확히 signature, key_id 순서다. 따라서 suffix나
    중복 critical field를 허용하지 않는다. business value는 서명 검증에 사용한 동일
    prefix에서만 만들어, framework QueryParams의 last-value 동작과 분리한다.
    """
    if not raw_query or len(raw_query) > _MAX_QUERY_LENGTH:
        return None
    try:
        raw_query_bytes = raw_query.encode("ascii")
    except UnicodeEncodeError:
        return None
    if _BAD_PERCENT_ESCAPE_RE.search(raw_query):
        return None

    fields = raw_query.split("&")
    if len(fields) < 3:
        return None
    signature_name, separator, signature = fields[-2].partition("=")
    if (
        signature_name != "signature"
        or not separator
        or _SIGNATURE_RE.fullmatch(signature) is None
    ):
        return None
    key_name, separator, key_id = fields[-1].partition("=")
    if key_name != "key_id" or not separator or _KEY_ID_RE.fullmatch(key_id) is None:
        return None

    signed_query = "&".join(fields[:-2])
    if not signed_query:
        return None
    try:
        pairs = parse_qsl(
            signed_query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=32,
        )
    except (UnicodeError, ValueError):
        return None

    parameters: dict[str, str] = {}
    seen_critical_fields: set[str] = set()
    for name, value in pairs:
        # signature/key_id가 prefix에도 있으면 envelope가 중복·모호하므로 거절한다.
        if name in {"signature", "key_id"}:
            return None
        if name in _CRITICAL_FIELDS:
            if name in seen_critical_fields:
                return None
            seen_critical_fields.add(name)
            if len(value) > _MAX_FIELD_LENGTHS[name] or "\x00" in value:
                return None
        elif len(name) > 128 or len(value) > 2048 or "\x00" in name or "\x00" in value:
            return None
        parameters[name] = value

    signed_content = raw_query_bytes[: len(signed_query)]
    return _SsvEnvelope(
        signed_content=signed_content,
        signature=signature,
        key_id=key_id,
        parameters=MappingProxyType(parameters),
    )


async def verify_and_parse(raw_query: str) -> VerifiedSsvPayload | None:
    """SSV envelope와 ECDSA를 검증하고 signed business payload만 반환한다."""
    envelope = _parse_envelope(raw_query)
    if envelope is None:
        return None
    try:
        pem = (await _get_keys()).get(envelope.key_id)
        if not pem:  # 캐시에 없는 key_id → Google 키 로테이션 대응 재조회
            pem = (await _get_keys(force=True)).get(envelope.key_id)
        if not pem:
            return None
        public_key = load_pem_public_key(pem.encode())
        signature = base64.urlsafe_b64decode(
            envelope.signature + "=" * (-len(envelope.signature) % 4)
        )
        public_key.verify(
            signature,
            envelope.signed_content,
            ec.ECDSA(hashes.SHA256()),
        )
        return VerifiedSsvPayload(
            key_id=envelope.key_id,
            parameters=envelope.parameters,
        )
    except Exception as e:  # noqa: BLE001  # 검증 실패는 조용히 거절
        _log.info("AdMob SSV 검증 실패: %r", e)
        return None
