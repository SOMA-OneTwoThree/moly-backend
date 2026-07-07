"""구독·IAP — 검증(증정)·충돌·환불회수·건초구매(멱등)·인증. StoreKit 디코드 mock."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.main import app
from app.services import app_store, hay_ledger, iap, subscription

UID = "11111111-1111-1111-1111-111111111111"
UID_UUID = uuid.UUID(UID)


class _Scalars:
    def __init__(self, items):
        self._items = items

    def first(self):
        return self._items[0] if self._items else None


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _Scalars(self._items)


class FakeSession:
    def __init__(self, exec_results=None):
        self.exec_results = list(exec_results or [])
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        return _Result(self.exec_results.pop(0) if self.exec_results else [])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def _patch_decode(monkeypatch, payload):
    monkeypatch.setattr(app_store, "decode", lambda s: payload)


# --- 구독 검증 ---
def test_plans_static():
    p = subscription.get_plans()
    assert {x["period"] for x in p["plans"]} == {"monthly", "yearly"}
    assert p["plans"][0]["hay_grant"] in (1000, 4000)


async def test_verify_first_time_grants(monkeypatch):
    _patch_decode(monkeypatch, {"productId": "app.moly.sub.monthly", "transactionId": "t1",
                                "originalTransactionId": "o1", "expiresDate": 1_800_000_000_000})

    async def _by(session, otx):
        return None

    async def _grant(session, uid, plan):
        return False

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, hay_balance=100)

    async def _apply(session, uid, t, amt, **kw):
        assert amt == 1000
        return 1100

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    out = await subscription.verify(FakeSession(), UID, "jws")
    assert out["plan"] == "monthly" and out["hay_granted"] == 1000 and out["balance_after"] == 1100


async def test_verify_unknown_product_422(monkeypatch):
    _patch_decode(monkeypatch, {"productId": "unknown", "transactionId": "t1"})
    with pytest.raises(AppError) as e:
        await subscription.verify(FakeSession(), UID, "jws")
    assert e.value.code == "RECEIPT_INVALID"


async def test_verify_conflict_other_user(monkeypatch):
    _patch_decode(monkeypatch, {"productId": "app.moly.sub.yearly", "transactionId": "t1",
                                "originalTransactionId": "o1"})

    async def _by(session, otx):
        return SimpleNamespace(user_id=uuid.uuid4())  # 다른 유저

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    with pytest.raises(AppError) as e:
        await subscription.verify(FakeSession(), UID, "jws")
    assert e.value.code == "RESTORE_CONFLICT"


async def test_verify_second_time_no_grant(monkeypatch):
    _patch_decode(monkeypatch, {"productId": "app.moly.sub.monthly", "transactionId": "t2",
                                "originalTransactionId": "o1", "expiresDate": 1_800_000_000_000})

    async def _by(session, otx):
        return SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active",
                               expires_at=None, latest_transaction_id="x")

    async def _grant(session, uid, plan):
        return True  # 이미 증정받음

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, hay_balance=500)

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    monkeypatch.setattr(subscription, "_load_profile", _lp)
    out = await subscription.verify(FakeSession(), UID, "jws")
    assert out["hay_granted"] == 0 and out["balance_after"] == 500


async def test_webhook_refund_revokes_and_clawback(monkeypatch):
    _patch_decode(monkeypatch, {
        "notificationType": "REFUND",
        "data": {"signedTransactionInfo": "inner"},
    })
    # 두 번째 decode(inner tx) 처리 위해 순차 반환
    payloads = iter([
        {"notificationType": "REFUND", "data": {"signedTransactionInfo": "inner"}},
        {"originalTransactionId": "o1", "transactionId": "t1"},
    ])
    monkeypatch.setattr(app_store, "decode", lambda s: next(payloads))
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active", expires_at=None)

    async def _by(session, otx):
        return sub

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, hay_balance=1500)

    clawed = {}

    async def _apply(session, uid, t, amt, **kw):
        clawed["amt"] = amt
        return 500

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    await subscription.handle_webhook(FakeSession(), "outer")
    assert sub.status == "revoked"
    assert clawed["amt"] == -1000  # min(증정 1000, 잔액 1500) 회수


# --- IAP 건초구매 ---
async def test_iap_purchase_success(monkeypatch):
    _patch_decode(monkeypatch, {"productId": "app.moly.hay.300", "transactionId": "t1"})
    pack = SimpleNamespace(id=uuid.uuid4(), hay_amount=300)
    session = FakeSession(exec_results=[[], [pack]])  # 기존없음, 팩있음

    async def _apply(session, uid, t, amt, **kw):
        assert amt == 300
        return 940

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    out = await iap.purchase(session, UID, "jws")
    assert out == {"amount": 300, "balance_after": 940}


async def test_iap_duplicate_409(monkeypatch):
    _patch_decode(monkeypatch, {"productId": "app.moly.hay.300", "transactionId": "t1"})
    session = FakeSession(exec_results=[[SimpleNamespace(id=1)]])  # 이미 처리됨
    with pytest.raises(AppError) as e:
        await iap.purchase(session, UID, "jws")
    assert e.value.code == "ALREADY_PROCESSED"


async def test_iap_unknown_product_422(monkeypatch):
    _patch_decode(monkeypatch, {"productId": "x", "transactionId": "t1"})
    session = FakeSession(exec_results=[[], []])  # 기존없음, 팩없음
    with pytest.raises(AppError) as e:
        await iap.purchase(session, UID, "jws")
    assert e.value.code == "RECEIPT_INVALID"


# --- 인증 ---
async def _dummy_session():
    yield None


@pytest.mark.parametrize("path,body", [
    ("/subscription", None),
    ("/wallet/purchases", {"signed_transaction": "x"}),
])
def test_subscription_endpoints_require_auth(path, body):
    app.dependency_overrides[get_session] = _dummy_session
    try:
        client = TestClient(app)
        r = client.get(path) if body is None else client.post(path, json=body)
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401


def test_plans_is_public():
    # 플랜 조회는 인증 불필요(정적)
    r = TestClient(app).get("/subscription/plans")
    assert r.status_code == 200
    assert "plans" in r.json()


# --- C1: StoreKit fail-closed 게이트(보안) ---
def test_app_store_fail_closed_in_production(monkeypatch):
    monkeypatch.setattr(app_store.settings, "environment", "production")
    with pytest.raises(AppError) as e:
        app_store.decode("anything")  # 서명검증 미구현 → 프로덕션 거부
    assert e.value.code == "RECEIPT_INVALID"


def test_app_store_decodes_in_local(monkeypatch):
    import jwt as _jwt

    monkeypatch.setattr(app_store.settings, "environment", "local")
    token = _jwt.encode({"productId": "x"}, "secret", algorithm="HS256")
    assert app_store.decode(token)["productId"] == "x"
