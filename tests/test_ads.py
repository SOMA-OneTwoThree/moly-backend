"""광고 — AdMob SSV 서명검증(실 ECDSA) + 세션 발급/자동 지급 흐름 + 인증."""
import base64
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.main import app
from app.services import ads, ads_ssv, economy, hay_ledger

UID = "11111111-1111-1111-1111-111111111111"
UID_UUID = uuid.UUID(UID)
SID = str(uuid.uuid4())
CONTENT = "ad_network=x&transaction_id=t1&custom_data=" + SID + "&reward_amount=1&timestamp=123"


# --- SSV 서명검증 ---
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


def _keys(d):
    async def f(**kw):
        return d
    return f


async def test_ssv_verify_valid(monkeypatch, signed):
    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({"1234": signed.pem}))
    assert await ads_ssv.verify(signed.raw_query, "1234", signed.sig_b64) is True


async def test_ssv_verify_tampered(monkeypatch, signed):
    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({"1234": signed.pem}))
    tampered = signed.raw_query.replace("reward_amount=1", "reward_amount=999")
    assert await ads_ssv.verify(tampered, "1234", signed.sig_b64) is False


async def test_ssv_verify_unknown_key(monkeypatch, signed):
    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({}))
    assert await ads_ssv.verify(signed.raw_query, "1234", signed.sig_b64) is False


# --- 세션 발급 / 자동 지급 ---
class FakeSession:
    def __init__(self, get_obj=None):
        self.get_obj = get_obj
        self.added = []
        self.committed = False

    async def get(self, model, key, **kw):
        return self.get_obj

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        if getattr(obj, "session_id", None) is None:
            obj.session_id = uuid.uuid4()


def _sess_row(**over):
    base = dict(session_id=uuid.UUID(SID), user_id=UID_UUID, activity_date=date(2026, 7, 5),
                granted=False, ssv_transaction_id=None)
    base.update(over)
    return SimpleNamespace(**base)


def _patch(monkeypatch, ad_count=3, balance=660):
    async def _daily(session, uid, ad):
        return SimpleNamespace(ad_reward_count=ad_count)

    async def _apply(session, uid, t, amt, **kw):
        return balance

    monkeypatch.setattr(economy, "_daily", _daily)
    monkeypatch.setattr(hay_ledger, "apply", _apply)


async def test_create_session_success(monkeypatch):
    _patch(monkeypatch, ad_count=3)

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, timezone="Asia/Seoul")

    monkeypatch.setattr(ads, "_load_profile", _lp)
    out = await ads.create_session(FakeSession(), UID)
    assert out["admob_user_id"] == UID
    assert out["views_used"] == 3 and out["views_limit"] == 10
    assert out["reward_session_id"]  # 발급됨


async def test_create_session_limit_429(monkeypatch):
    _patch(monkeypatch, ad_count=10)

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, timezone="Asia/Seoul")

    monkeypatch.setattr(ads, "_load_profile", _lp)
    from app.core.errors import AppError
    with pytest.raises(AppError) as e:
        await ads.create_session(FakeSession(), UID)
    assert e.value.code == "AD_LIMIT_REACHED"


async def test_grant_success(monkeypatch):
    _patch(monkeypatch, ad_count=3)
    row = _sess_row()
    s = FakeSession(get_obj=row)
    assert await ads.grant_from_ssv(s, SID, "t1") == "granted"
    assert row.granted is True and row.ssv_transaction_id == "t1" and s.committed


async def test_grant_already_granted_skip(monkeypatch):
    async def _apply(*a, **k):
        raise AssertionError("이미 지급된 세션 재지급 금지")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    row = _sess_row(granted=True)
    s = FakeSession(get_obj=row)
    assert await ads.grant_from_ssv(s, SID, "t2") == "duplicate"  # 재전송 — 무시
    assert s.committed is False


async def test_grant_limit_no_pay(monkeypatch):
    async def _apply(*a, **k):
        raise AssertionError("한도 초과 시 지급 금지")

    async def _daily(session, uid, ad):
        return SimpleNamespace(ad_reward_count=10)

    monkeypatch.setattr(economy, "_daily", _daily)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    row = _sess_row()
    assert await ads.grant_from_ssv(FakeSession(get_obj=row), SID, "t1") == "daily_limit"
    assert row.granted is False  # 미지급


async def test_grant_session_not_found_skip():
    s = FakeSession(get_obj=None)
    assert await ads.grant_from_ssv(s, SID, "t1") == "session_not_found"  # 무시, 에러 없음
    assert s.committed is False


async def test_grant_bad_session_id_skip():
    s = FakeSession()
    assert await ads.grant_from_ssv(s, "not-a-uuid", "t1") == "invalid_session"  # 형식 오류
    assert s.committed is False


async def test_grant_transaction_conflict_rollback(monkeypatch):
    """같은 transaction_id가 다른 세션으로 이미 지급 — UNIQUE 충돌 롤백, 멱등."""
    from sqlalchemy.exc import IntegrityError

    _patch(monkeypatch, ad_count=3)

    class ConflictSession(FakeSession):
        rolled_back = False

        async def commit(self):
            raise IntegrityError("stmt", {}, Exception("duplicate key"))

        async def rollback(self):
            self.rolled_back = True

    s = ConflictSession(get_obj=_sess_row())
    assert await ads.grant_from_ssv(s, SID, "t1") == "transaction_conflict"
    assert s.rolled_back is True


# --- 엔드포인트 ---
async def _dummy_session():
    yield None


def test_ssv_webhook_missing_params():
    r = TestClient(app).get("/webhooks/ad-ssv?key_id=1")
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"


@pytest.mark.parametrize("outcome", ["granted", "session_not_found"])
def test_ssv_webhook_result_in_body(monkeypatch, outcome):
    """서명 통과 후 처리 결과는 HTTP 200 유지 + body result로 구분."""
    async def _verify(*a, **k):
        return True

    async def _grant(session, sid, tx):
        return outcome

    monkeypatch.setattr(ads_ssv, "verify", _verify)
    monkeypatch.setattr(ads, "grant_from_ssv", _grant)
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).get(
            f"/webhooks/ad-ssv?custom_data={SID}&transaction_id=t1&signature=s&key_id=1"
        )
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and r.json() == {"status": "ok", "result": outcome}


def test_reward_ad_session_requires_auth():
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).post("/reward-ad-sessions")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
