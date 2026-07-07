"""Supabase 액세스 토큰 검증 — 유저 컨텍스트 확보.

⚠️ 검증 방식(로컬 JWKS vs remote getUser)은 **auth 모듈 설계 단계에서 확정**.
현재는 moly-voice 검증본을 이관한 remote getUser 폴백(검증됨 — 만료/폐기 토큰까지 잡힘).
ARCHITECTURE §8은 JWKS 로컬 검증을 지향(요청당 네트워크 제거) → 설계 확정 시 교체.
전 엔드포인트가 Bearer 필요(API_SPEC 1장, 웹훅·GET /app-config 제외).
"""
from __future__ import annotations

import logging

import httpx
from fastapi import Header, HTTPException

from app.config import settings

_log = logging.getLogger("moly-backend")


async def verify_supabase_token(token: str) -> str | None:
    """Supabase access token → user_id. 무효/오류/미설정이면 None."""
    if not (settings.supabase_url and settings.supabase_anon_key):
        _log.warning("SUPABASE_URL/ANON_KEY 미설정 — 토큰 검증 불가")
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{settings.supabase_url}/auth/v1/user",
                headers={
                    "apikey": settings.supabase_anon_key,
                    "Authorization": f"Bearer {token}",
                },
            )
    except Exception as e:  # noqa: BLE001  # 네트워크/DNS/타임아웃 — 거절
        _log.warning("토큰 검증 오류: %r", e)
        return None
    if r.status_code != 200:  # 401=무효/만료, 그 외=Supabase 오류 — 모두 거절
        _log.info("토큰 검증 실패: HTTP %s", r.status_code)
        return None
    uid = (r.json() or {}).get("id")
    return uid or None


async def get_current_user(authorization: str | None = Header(default=None)) -> str:
    """FastAPI 의존성 — Bearer 토큰 검증 후 user_id 반환. 실패 = 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    uid = await verify_supabase_token(token)
    if not uid:
        raise HTTPException(status_code=401, detail="invalid token")
    return uid
