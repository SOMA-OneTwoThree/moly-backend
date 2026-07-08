"""구독 — RevenueCat 웹훅 이벤트 매핑(증정·환불회수·만료·유예·건초IAP)·인증. DB mock."""
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.db import get_session
from app.main import app
from app.services import hay_ledger, iap, subscription

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


def test_plans_static():
    p = subscription.get_plans()
    assert {x["period"] for x in p["plans"]} == {"monthly", "yearly"}
    assert p["plans"][0]["hay_grant"] in (1000, 4000)


# --- RevenueCat 웹훅 이벤트 매핑 ---
def _rc_event(**over):
    e = {
        "type": "INITIAL_PURCHASE",
        "app_user_id": UID,
        "product_id": "app.moly.sub.monthly",
        "original_transaction_id": "o-rc-1",
        "transaction_id": "t-rc-1",
        "expiration_at_ms": 1_900_000_000_000,
        "environment": "PRODUCTION",
    }
    e.update(over)
    return e


async def test_rc_initial_purchase_creates_and_grants(monkeypatch):
    granted = {}

    async def _by(session, otx, lock=False):
        return None

    async def _grant(session, uid, plan):
        return False

    async def _apply(session, uid, t, amt, **kw):
        granted["amt"] = amt
        return 1000

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession()
    await subscription.handle_revenuecat_event(s, _rc_event())
    assert any(getattr(o, "status", None) == "active" for o in s.added)  # 구독 생성
    assert granted["amt"] == 1000 and s.committed


async def test_rc_second_time_no_grant(monkeypatch):
    async def _by(session, otx, lock=False):
        return SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active",
                               expires_at=None, auto_renew_enabled=True, latest_transaction_id=None)

    async def _grant(session, uid, plan):
        return True  # 이미 증정함

    async def _apply(session, uid, t, amt, **kw):
        raise AssertionError("재증정하면 안 됨")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    await subscription.handle_revenuecat_event(FakeSession(), _rc_event(type="RENEWAL"))


async def test_rc_cancellation_refund_clawback(monkeypatch):
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active")
    clawed = {}

    async def _by(session, otx, lock=False):
        return sub

    async def _claw(session, uid, otx):
        return False

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, hay_balance=1000)

    async def _uneq(session, user_id):
        pass

    async def _apply(session, uid, t, amt, **kw):
        clawed["amt"] = amt
        return 0

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_clawback_done", _claw)
    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(subscription, "_unequip_subscriber_only", _uneq)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    await subscription.handle_revenuecat_event(
        FakeSession(), _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT")
    )
    assert sub.status == "revoked" and clawed["amt"] == -1000  # min(1000 증정, 1000 잔액) 회수


async def test_rc_cancellation_unsubscribe_only_autorenew_off(monkeypatch):
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active", auto_renew_enabled=True)

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    await subscription.handle_revenuecat_event(
        FakeSession(), _rc_event(type="CANCELLATION", cancel_reason="UNSUBSCRIBE")
    )
    assert sub.auto_renew_enabled is False and sub.status == "active"  # 만료 전까지 혜택 유지


async def test_rc_expiration_unequips(monkeypatch):
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active")
    unequipped = {}

    async def _by(session, otx, lock=False):
        return sub

    async def _uneq(session, user_id):
        unequipped["done"] = True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_unequip_subscriber_only", _uneq)
    await subscription.handle_revenuecat_event(FakeSession(), _rc_event(type="EXPIRATION"))
    assert sub.status == "expired" and unequipped.get("done")


async def test_rc_billing_issue_grace(monkeypatch):
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active")

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    await subscription.handle_revenuecat_event(FakeSession(), _rc_event(type="BILLING_ISSUE"))
    assert sub.status == "grace_period"


async def test_rc_non_renewing_grants_hay(monkeypatch):
    called = {}

    async def _grant(session, uid, product_id, transaction_id):
        called["pid"], called["tx"] = product_id, transaction_id

    monkeypatch.setattr(iap, "grant_pack", _grant)
    await subscription.handle_revenuecat_event(
        FakeSession(),
        _rc_event(type="NON_RENEWING_PURCHASE",
                  product_id="com.geniusjun.moly.hay.300", transaction_id="tx-hay-1"),
    )
    assert called["pid"] == "com.geniusjun.moly.hay.300" and called["tx"] == "tx-hay-1"


async def test_rc_bad_app_user_id_skips():
    s = FakeSession()
    await subscription.handle_revenuecat_event(s, _rc_event(app_user_id="not-a-uuid"))
    assert s.committed is False  # 매핑 불가 → 아무것도 안 함


def test_rc_webhook_auth_required(monkeypatch):
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "revenuecat_webhook_auth", "sekret")

    async def _sess():
        yield None

    app.dependency_overrides[get_session] = _sess
    try:
        c = TestClient(app)
        assert c.post("/webhooks/revenuecat", json={"event": {}}).status_code == 401  # 헤더 없음
        assert c.post("/webhooks/revenuecat", json={"event": {}},
                      headers={"Authorization": "wrong"}).status_code == 401  # 불일치
    finally:
        app.dependency_overrides.clear()


def test_rc_webhook_auth_ok_calls_handler(monkeypatch):
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "revenuecat_webhook_auth", "sekret")
    called = {}

    async def _spy(session, event):
        called["event"] = event

    async def _sess():
        yield None

    monkeypatch.setattr(subscription, "handle_revenuecat_event", _spy)
    app.dependency_overrides[get_session] = _sess
    try:
        r = TestClient(app).post("/webhooks/revenuecat", json={"event": {"type": "TEST"}},
                                 headers={"Authorization": "sekret"})
        assert r.status_code == 200 and called["event"] == {"type": "TEST"}
    finally:
        app.dependency_overrides.clear()
