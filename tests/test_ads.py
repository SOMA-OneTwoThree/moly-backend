"""광고 — AdMob SSV 서명검증(실 ECDSA) + 보상 수령 흐름/에러 + 인증."""
import base64
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.main import app
from app.services import ads, ads_ssv, economy, hay_ledger

UID = "11111111-1111-1111-1111-111111111111"
UID_UUID = uuid.UUID(UID)
CONTENT = "ad_network=x&transaction_id=t1&custom_data=" + UID + "&reward_amount=1&timestamp=123"


@pytest.fixture
def signed():
    priv = ec.generate_private_key(ec.SECP256R1())
    sig = priv.sign(CONTENT.encode(), ec.ECDSA(hashes.SHA256()))
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    raw_query = f"{CONTENT}&signature={sig_b64}&key_id=1234"
    return SimpleNamespace(sig_b64=sig_b64, pem=pem, raw_query=raw_query)


async def test_ssv_verify_valid(monkeypatch, signed):
    async def _keys():
        return {"1234": signed.pem}

    monkeypatch.setattr(ads_ssv, "_get_keys", _keys)
    assert await ads_ssv.verify(signed.raw_query, "1234", signed.sig_b64) is True


async def test_ssv_verify_tampered_content(monkeypatch, signed):
    async def _keys():
        return {"1234": signed.pem}

    monkeypatch.setattr(ads_ssv, "_get_keys", _keys)
    tampered = signed.raw_query.replace("reward_amount=1", "reward_amount=999")
    assert await ads_ssv.verify(tampered, "1234", signed.sig_b64) is False


async def test_ssv_verify_unknown_key(monkeypatch, signed):
    async def _keys():
        return {}

    monkeypatch.setattr(ads_ssv, "_get_keys", _keys)
    assert await ads_ssv.verify(signed.raw_query, "1234", signed.sig_b64) is False


# --- 보상 수령 ---
class FakeSession:
    def __init__(self, get_obj=None):
        self.get_obj = get_obj
        self.committed = False

    async def get(self, model, key, **kw):
        return self.get_obj

    async def commit(self):
        self.committed = True


def _rec(**over):
    base = dict(user_id=UID_UUID, granted=False, activity_date=date(2026, 7, 5))
    base.update(over)
    return SimpleNamespace(**base)


def _patch_claim(monkeypatch, ad_count=3, balance=650):
    async def _daily(session, uid, ad):
        return SimpleNamespace(ad_reward_count=ad_count)

    async def _apply(session, uid, t, amt, **kw):
        return balance

    monkeypatch.setattr(economy, "_daily", _daily)
    monkeypatch.setattr(hay_ledger, "apply", _apply)


async def test_claim_success(monkeypatch):
    _patch_claim(monkeypatch, ad_count=3)
    rec = _rec()
    out = await ads.claim(FakeSession(get_obj=rec), UID, "t1")
    assert out["granted"] == 10 and out["views_used"] == 4
    assert rec.granted is True


async def test_claim_no_record_422(monkeypatch):
    with pytest.raises(AppError) as e:
        await ads.claim(FakeSession(get_obj=None), UID, "t1")
    assert e.value.code == "AD_VERIFY_FAILED"


async def test_claim_already_processed_409(monkeypatch):
    with pytest.raises(AppError) as e:
        await ads.claim(FakeSession(get_obj=_rec(granted=True)), UID, "t1")
    assert e.value.code == "ALREADY_PROCESSED"


async def test_claim_limit_reached_429(monkeypatch):
    _patch_claim(monkeypatch, ad_count=10)  # 이미 10회
    with pytest.raises(AppError) as e:
        await ads.claim(FakeSession(get_obj=_rec()), UID, "t1")
    assert e.value.code == "AD_LIMIT_REACHED"


# --- 엔드포인트 ---
async def _dummy_session():
    yield None


def test_ssv_webhook_missing_params():
    r = TestClient(app).get("/webhooks/ad-ssv?key_id=1")  # signature 등 누락
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION"


def test_ads_reward_requires_auth():
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).post("/ads/reward", json={"ssv_transaction_id": "t1"})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
