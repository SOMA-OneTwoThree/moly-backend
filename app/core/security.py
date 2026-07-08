"""Supabase JWT 검증 — 로컬 JWKS(비대칭 서명) 검증.

요청당 네트워크 호출 없이 JWKS 공개키로 서명을 로컬 검증(ARCHITECTURE §8).
키는 최초 1회 페치 후 캐시(PyJWKClient). 전 엔드포인트 Bearer 필요
(API_SPEC 1장, 웹훅·GET /app-config 제외). 검증 실패 = 401.
"""
from __future__ import annotations

import asyncio
import logging

import jwt
from fastapi import Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import settings
from app.core.errors import unauthorized

_log = logging.getLogger("moly-backend")

# Swagger UI "Authorize" 버튼용 Bearer 스킴. auto_error=False → 미/오류 토큰은
# 우리 표준 401 봉투(unauthorized)로 처리(프론트 계약 유지, 403 기본동작 회피).
_bearer_scheme = HTTPBearer(auto_error=False, description="Supabase access token (Bearer)")

# Supabase JWT: aud="authenticated", 비대칭 알고리즘(ES256/RS256).
_ALGORITHMS = ["ES256", "RS256"]
_AUDIENCE = "authenticated"
# 클럭 스큐 허용(초) — 서버-클라 시계 차로 갓 발급된 토큰의 iat/nbf가 "미래"로 보이거나
# exp가 경계에서 조기 만료되는 것을 방지(표준 관행). 실측 스큐는 수 초 수준.
_LEEWAY_SECONDS = 60

_jwks_client: PyJWKClient | None = None


def _jwks_url() -> str:
    if settings.supabase_jwks_url:
        return settings.supabase_jwks_url
    if settings.supabase_url:
        return f"{settings.supabase_url}/auth/v1/.well-known/jwks.json"
    return ""


def _get_client() -> PyJWKClient | None:
    global _jwks_client
    if _jwks_client is None:
        url = _jwks_url()
        if not url:
            return None
        _jwks_client = PyJWKClient(url, cache_keys=True)
    return _jwks_client


def _verify_sync(token: str) -> str | None:
    client = _get_client()
    if client is None:
        _log.warning("SUPABASE_JWKS_URL/URL 미설정 — 토큰 검증 불가")
        return None
    # iss 검증(심층방어) — 설정된 경우에만. Supabase iss = "<url>/auth/v1".
    issuer = f"{settings.supabase_url}/auth/v1" if settings.supabase_url else None
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=_ALGORITHMS,
            audience=_AUDIENCE,
            issuer=issuer,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["sub", "exp"]},
        )
    except Exception as e:  # noqa: BLE001  # 서명/만료/형식 오류 — 모두 거절
        _log.info("토큰 검증 실패: %r", e)
        return None
    # 익명 로그인 거부(소셜 전용) — is_anonymous 토큰 차단. 어뷰징(무한 계정·건초 파밍) 방지.
    if claims.get("is_anonymous") is True and not settings.allow_anonymous_auth:
        _log.info("익명 유저 토큰 거부(소셜 로그인 전용)")
        return None
    return claims.get("sub")


async def verify_supabase_token(token: str) -> str | None:
    """access token → user_id(sub). 무효/오류/미설정이면 None. (동기 검증을 스레드로 오프로드)"""
    return await asyncio.to_thread(_verify_sync, token)


async def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> str:
    """FastAPI 의존성 — Bearer 토큰 검증 후 user_id 반환. 실패 = 401.

    HTTPBearer 스킴으로 받아 Swagger에 Authorize 버튼이 노출된다. 검증 로직은 동일.
    """
    if cred is None or (cred.scheme or "").lower() != "bearer":
        raise unauthorized()
    uid = await verify_supabase_token(cred.credentials.strip())
    if not uid:
        raise unauthorized()
    return uid
