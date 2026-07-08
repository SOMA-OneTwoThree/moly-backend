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
    monkeypatch.setattr(app_store, "decode_transaction", lambda s: payload)


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


async def test_verify_expired_receipt_no_grant(monkeypatch):
    # 만료된 영수증(expiresDate 과거)으로 verify 시 증정 미지급 + status expired (#4)
    _patch_decode(monkeypatch, {"productId": "app.moly.sub.monthly", "transactionId": "t1",
                                "originalTransactionId": "o1", "expiresDate": 1_000_000_000_000})

    async def _by(session, otx, lock=False):
        return None

    async def _grant(session, uid, plan):
        return False

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, hay_balance=100)

    async def _apply(session, uid, t, amt, **kw):
        raise AssertionError("만료 영수증엔 증정 지급 금지")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    out = await subscription.verify(FakeSession(), UID, "jws")
    assert out["status"] == "expired" and out["hay_granted"] == 0 and out["balance_after"] == 100


async def test_verify_does_not_resurrect_revoked(monkeypatch):
    # 환불(revoked)된 구독은 유효 서명거래 재전송으로도 되살아나지 않음 (#1)
    _patch_decode(monkeypatch, {"productId": "app.moly.sub.monthly", "transactionId": "t9",
                                "originalTransactionId": "o1", "expiresDate": 1_800_000_000_000})
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="revoked", expires_at=None)

    async def _by(session, otx, lock=False):
        return sub

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, hay_balance=0)

    async def _apply(session, uid, t, amt, **kw):
        raise AssertionError("revoked 구독엔 증정 지급 금지")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    out = await subscription.verify(FakeSession(), UID, "jws")
    assert sub.status == "revoked"  # 상태 유지(active로 덮이지 않음)
    assert out["status"] == "revoked" and out["hay_granted"] == 0


async def test_restore_reactivates_subscription(monkeypatch):
    _patch_decode(monkeypatch, {"productId": "app.moly.sub.monthly", "transactionId": "t",
                                "originalTransactionId": "o1", "expiresDate": 1_800_000_000_000})

    async def _by(session, otx):
        return None  # 우리 DB에 기록 없음(웹훅 유실) → 재활성

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, trial_ends_at=None)

    async def _latest(session, uid):
        return SimpleNamespace(status="active", plan="monthly", auto_renew_enabled=True, expires_at=None)

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(subscription, "_latest_sub", _latest)
    session = FakeSession()
    out = await subscription.restore(session, UID, ["jws"])
    assert out["status"] == "active" and out["plan"] == "monthly"
    assert session.added  # 구독 레코드 생성됨(단순 상태반환 아님)


async def test_restore_conflict_other_user(monkeypatch):
    _patch_decode(monkeypatch, {"productId": "app.moly.sub.monthly", "transactionId": "t",
                                "originalTransactionId": "o1"})

    async def _by(session, otx):
        return SimpleNamespace(user_id=uuid.uuid4())  # 다른 계정 소유

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    with pytest.raises(AppError) as e:
        await subscription.restore(FakeSession(), UID, ["jws"])
    assert e.value.code == "RESTORE_CONFLICT"


async def test_webhook_refund_revokes_and_clawback(monkeypatch):
    # 알림(decode_notification)과 내부 거래(decode_transaction)를 각각 패치
    monkeypatch.setattr(app_store, "decode_notification", lambda s: {
        "notificationType": "REFUND",
        "data": {"signedTransactionInfo": "inner"},
    })
    monkeypatch.setattr(app_store, "decode_transaction", lambda s: {
        "originalTransactionId": "o1", "transactionId": "t1",
    })
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active", expires_at=None)

    async def _by(session, otx, lock=False):
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


async def test_webhook_refund_idempotent_no_double_clawback(monkeypatch):
    # 이미 회수 원장이 있으면(재생·재시도·REVOKE→REFUND) 재회수하지 않음 (#3)
    monkeypatch.setattr(app_store, "decode_notification", lambda s: {
        "notificationType": "REFUND",
        "data": {"signedTransactionInfo": "inner"},
    })
    monkeypatch.setattr(app_store, "decode_transaction", lambda s: {
        "originalTransactionId": "o1", "transactionId": "t1",
    })
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active", expires_at=None)

    async def _by(session, otx, lock=False):
        return sub

    async def _apply(session, uid, t, amt, **kw):
        raise AssertionError("이미 회수됨 — 재회수 금지")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    # exec_results: [unequip delete, _clawback_done select→기존 원장 있음]
    session = FakeSession(exec_results=[[], [SimpleNamespace(id=1)]])
    await subscription.handle_webhook(session, "outer")
    assert sub.status == "revoked"  # 상태는 반영, 회수는 스킵


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


# --- C1: StoreKit 검증 게이트(보안) ---
def test_app_store_fail_closed_in_production(monkeypatch):
    # 검증기 미설정(bundle_id 없음) + 비로컬 → fail-closed
    monkeypatch.setattr(app_store.settings, "app_store_bundle_id", "")
    app_store._verifier.cache_clear()
    monkeypatch.setattr(app_store.settings, "environment", "production")
    with pytest.raises(AppError) as e:
        app_store.decode_transaction("anything")
    assert e.value.code == "RECEIPT_INVALID"


def test_app_store_decodes_in_local(monkeypatch):
    import jwt as _jwt

    monkeypatch.setattr(app_store.settings, "app_store_bundle_id", "")
    app_store._verifier.cache_clear()
    monkeypatch.setattr(app_store.settings, "environment", "local")
    token = _jwt.encode({"productId": "x"}, "secret", algorithm="HS256")
    assert app_store.decode_transaction(token)["productId"] == "x"


def test_app_store_rejects_bad_signature_when_configured(monkeypatch):
    # bundle_id 설정 → 실제 x5c 검증기 가동. 위조/HS256 토큰은 서명검증 실패로 거부.
    monkeypatch.setattr(app_store.settings, "app_store_bundle_id", "com.geniusjun.moly")
    monkeypatch.setattr(app_store.settings, "app_store_environment", "Sandbox")
    app_store._verifier.cache_clear()
    import jwt as _jwt

    bogus = _jwt.encode({"productId": "x"}, "secret", algorithm="HS256")
    with pytest.raises(AppError) as e:
        app_store.decode_transaction(bogus)
    assert e.value.code == "RECEIPT_INVALID"
    app_store._verifier.cache_clear()  # 다른 테스트 오염 방지
