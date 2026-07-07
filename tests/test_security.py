"""JWT 검증 토대 테스트 — 실제 ES256 서명/검증 경로를 태워 확인(JWKS 페치만 스텁)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.core import security
from app.core.errors import AppError


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


@pytest.fixture
def ec_keys():
    priv = ec.generate_private_key(ec.SECP256R1())
    return priv, priv.public_key()


def _make_token(priv, **claim_overrides) -> str:
    from app.config import settings

    claims = {
        "sub": "user-123",
        "aud": "authenticated",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    if settings.supabase_url:  # .env에 URL 있으면 iss 검증됨 → 맞춰서 통과
        claims["iss"] = f"{settings.supabase_url}/auth/v1"
    claims.update(claim_overrides)
    return jwt.encode(claims, priv, algorithm="ES256")


def _patch_client(monkeypatch, pub):
    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey(pub)

    monkeypatch.setattr(security, "_get_client", lambda: _Client())


async def test_valid_token_returns_sub(monkeypatch, ec_keys):
    priv, pub = ec_keys
    _patch_client(monkeypatch, pub)
    assert await security.verify_supabase_token(_make_token(priv)) == "user-123"


async def test_expired_token_rejected(monkeypatch, ec_keys):
    priv, pub = ec_keys
    _patch_client(monkeypatch, pub)
    token = _make_token(priv, exp=datetime.now(timezone.utc) - timedelta(hours=1))
    assert await security.verify_supabase_token(token) is None


async def test_wrong_audience_rejected(monkeypatch, ec_keys):
    priv, pub = ec_keys
    _patch_client(monkeypatch, pub)
    assert await security.verify_supabase_token(_make_token(priv, aud="other")) is None


async def test_missing_jwks_config_returns_none(monkeypatch):
    monkeypatch.setattr(security, "_get_client", lambda: None)
    assert await security.verify_supabase_token("whatever") is None


async def test_get_current_user_requires_bearer(monkeypatch):
    with pytest.raises(AppError) as e:
        await security.get_current_user(authorization=None)
    assert e.value.code == "UNAUTHORIZED"
    assert e.value.http_status == 401


async def test_get_current_user_returns_uid(monkeypatch):
    async def _fake_verify(token: str):
        return "user-xyz"

    monkeypatch.setattr(security, "verify_supabase_token", _fake_verify)
    assert await security.get_current_user(authorization="Bearer abc.def.ghi") == "user-xyz"
