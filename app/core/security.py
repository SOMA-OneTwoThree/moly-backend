"""Supabase JWT 검증 — 로컬 JWKS(비대칭 서명) 검증.

요청당 네트워크 호출 없이 JWKS 공개키로 서명을 로컬 검증(ARCHITECTURE §8).
키는 최초 1회 페치 후 캐시(PyJWKClient). 전 엔드포인트 Bearer 필요
(API_SPEC 1장, 웹훅·GET /app-config 제외). 검증 실패 = 401.
"""
from __future__ import annotations

import asyncio
import logging

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

from app.config import settings

_log = logging.getLogger("moly-backend")

# Supabase JWT: aud="authenticated", 비대칭 알고리즘(ES256/RS256).
_ALGORITHMS = ["ES256", "RS256"]
_AUDIENCE = "authenticated"

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
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=_ALGORITHMS,
            audience=_AUDIENCE,
            options={"require": ["sub", "exp"]},
        )
    except Exception as e:  # noqa: BLE001  # 서명/만료/형식 오류 — 모두 거절
        _log.info("토큰 검증 실패: %r", e)
        return None
    return claims.get("sub")


async def verify_supabase_token(token: str) -> str | None:
    """access token → user_id(sub). 무효/오류/미설정이면 None. (동기 검증을 스레드로 오프로드)"""
    return await asyncio.to_thread(_verify_sync, token)


async def get_current_user(authorization: str | None = Header(default=None)) -> str:
    """FastAPI 의존성 — Bearer 토큰 검증 후 user_id 반환. 실패 = 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    uid = await verify_supabase_token(token)
    if not uid:
        raise HTTPException(status_code=401, detail="invalid token")
    return uid
