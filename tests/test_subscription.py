"""구독 — RevenueCat 웹훅 이벤트 매핑(증정·환불회수·만료·유예·건초IAP)·인증. DB mock."""
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.security import get_current_user
from app.main import app
from app.services import hay_ledger, payment, subscription

UID = "11111111-1111-1111-1111-111111111111"
UID_UUID = uuid.UUID(UID)


class _Scalars:
    def __init__(self, items):
        self._items = items

    def first(self):
        return self._items[0] if self._items else None

    def __iter__(self):  # get_config_values 등 scalars() 순회 대응
        return iter(self._items)


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _Scalars(self._items)


class _Nested:
    """begin_nested()(SAVEPOINT) 흉내 — 실 DB의 savepoint 롤백은 없고 예외만 전파(단위 테스트)."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False  # 예외 전파(handler의 try/except IntegrityError가 잡음)


class FakeSession:
    def __init__(self, exec_results=None, get_map=None):
        self.exec_results = list(exec_results or [])
        self.get_map = dict(get_map or {})  # Model 클래스 → get() 반환 객체
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt):
        return _Result(self.exec_results.pop(0) if self.exec_results else [])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def flush(self):
        pass

    async def get(self, model, key, **kw):
        return self.get_map.get(model)

    def begin_nested(self):
        return _Nested()


async def test_plans_static(monkeypatch):
    async def _lp(session, uid):
        return SimpleNamespace(language="ko")

    monkeypatch.setattr(subscription, "_load_profile", _lp)
    p = await subscription.get_plans(FakeSession(), UID)
    assert {x["period"] for x in p["plans"]} == {"monthly", "yearly"}
    assert p["plans"][0]["hay_grant"] in (1000, 4000)
    assert "대화 한도 확장" in p["benefits"]  # ko 기본


async def test_plans_benefits_localized(monkeypatch):
    """구독 혜택 문구가 유저 언어로 반환(BCP47 en-US→en 정규화) — SOMA-346."""
    async def _lp(session, uid):
        return SimpleNamespace(language="en-US")

    monkeypatch.setattr(subscription, "_load_profile", _lp)
    p = await subscription.get_plans(FakeSession(), UID)
    assert "Extended chat limit" in p["benefits"] and "대화 한도 확장" not in p["benefits"]


async def test_plans_benefits_ja(monkeypatch):
    """구독 혜택이 일본어로 반환(SOMA-361)."""
    async def _lp(session, uid):
        return SimpleNamespace(language="ja")

    monkeypatch.setattr(subscription, "_load_profile", _lp)
    p = await subscription.get_plans(FakeSession(), UID)
    assert "会話の上限アップ" in p["benefits"] and "대화 한도 확장" not in p["benefits"]


def test_plans_require_auth():
    async def _sess():
        yield None

    app.dependency_overrides[get_session] = _sess
    try:
        response = TestClient(app).get("/subscription/plans")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_plans_authenticated(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: UID

    async def _sess():
        yield None

    async def _lp(session, uid):
        return SimpleNamespace(language="ko")

    monkeypatch.setattr(subscription, "_load_profile", _lp)
    app.dependency_overrides[get_session] = _sess
    try:
        response = TestClient(app).get("/subscription/plans")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert {plan["period"] for plan in response.json()["plans"]} == {"monthly", "yearly"}


# --- RevenueCat 웹훅 이벤트 매핑(SOMA-372 재작성: HandlerResult 반환·내부 commit 없음) ---
from app.models.hay_transaction import HayTransaction  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.subscription import Subscription  # noqa: E402
from app.services.subscription import (  # noqa: E402
    DEPENDENCY_MISSING,
    HANDLED,
    NO_OP,
    PERMANENT_FAILURE,
    TRANSFER_MANUAL,
)

SUB_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ORD_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class _FakeOrig(Exception):
    """asyncpg IntegrityError.orig 흉내(sqlstate + constraint_name)."""
    def __init__(self, sqlstate, constraint_name):
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def _rc_event(**over):
    e = {
        "id": "evt-1",
        "type": "INITIAL_PURCHASE",
        "app_user_id": UID,
        "product_id": "app.moly.sub.monthly",
        "original_transaction_id": "o-rc-1",
        "transaction_id": "t-rc-1",
        "expiration_at_ms": 1_900_000_000_000,
        "event_timestamp_ms": 1_800_000_000_000,
        "environment": "PRODUCTION",
        "store": "APP_STORE",
    }
    e.update(over)
    return e


def _sub(**over):
    base = dict(id=SUB_ID, user_id=UID_UUID, plan="monthly", status="active",
                expires_at=None, auto_renew_enabled=True, latest_transaction_id=None,
                last_event_at=None)
    base.update(over)
    return SimpleNamespace(**base)


async def test_rc_initial_purchase_creates_and_grants(monkeypatch):
    granted = {}

    async def _by(session, otx, lock=False):
        return None

    async def _pe(session, tx):
        return False

    async def _grant(session, uid, plan):
        return False

    async def _apply(session, uid, t, amt, **kw):
        granted["amt"] = amt
        return SimpleNamespace(id=1, balance_after=1000)

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription.payment, "payment_exists", _pe)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession()
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(price_in_purchased_currency=5900, currency="KRW"))
    assert res.outcome == HANDLED
    assert any(getattr(o, "status", None) == "active" for o in s.added)  # 구독 생성
    assert granted["amt"] == 1000
    assert any(getattr(o, "hay_transaction_id", None) == 1 for o in s.added)  # 증정 원장 연결
    pay = next(o for o in s.added if getattr(o, "store_transaction_id", None) == "t-rc-1")
    assert pay.amount == 5900 and pay.status == "paid" and pay.subscription_id is not None


async def test_rc_renewal_no_regrant(monkeypatch):
    """RENEWAL 재수신 — 결제·증정 멱등(재차감 없음). 같은 유저·구독 결제면 재기록 없이 no-op."""
    sub = _sub()
    same_pay = SimpleNamespace(user_id=UID_UUID, order_id=None, subscription_id=SUB_ID)

    async def _by(session, otx, lock=False):
        return sub

    async def _grant(session, uid, plan):
        return True  # 이미 증정함

    async def _apply(*a, **k):
        raise AssertionError("재증정하면 안 됨")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession(exec_results=[[same_pay]])  # _payment_by_tx 사전 재조회 → 같은 유저·구독(멱등)
    res = await subscription.handle_revenuecat_event(s, _rc_event(type="RENEWAL"))
    assert res.outcome == HANDLED
    assert not any(getattr(o, "store_transaction_id", None) for o in s.added)  # 결제 재기록 없음


# --- 환불 회수(§4): 정확 transaction_id로 Payment 잠금, 실 원장액 권위, 음수 부채 ---
async def test_rc_refund_subscription_clawback(monkeypatch):
    """CUSTOMER_SUPPORT 환불 → Payment 기준 대상 유저·구독 revoke + 실 증정 원장액 음수 회수."""
    pay = SimpleNamespace(store_transaction_id="t-rc-1", user_id=UID_UUID,
                          order_id=None, subscription_id=SUB_ID, status="paid")
    sub = _sub(status="active")
    grant = SimpleNamespace(id=uuid.uuid4(), revoked_at=None, clawback_hay_transaction_id=None,
                            hay_transaction_id=99)
    grant_tx = SimpleNamespace(amount=1000)
    clawed = {}

    async def _apply(session, uid, t, amt, **kw):
        clawed.update(t=t, amt=amt, allow=kw.get("allow_negative"))
        return SimpleNamespace(id=7)

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    # exec: [0] _payment_by_tx → pay, [1] clawback grant 조회 → grant
    s = FakeSession(exec_results=[[pay], [grant]],
                    get_map={Subscription: sub, HayTransaction: grant_tx})
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"))
    assert res.outcome == HANDLED
    assert sub.status == "revoked" and pay.status == "refunded"
    assert clawed == {"t": "refund_revoke", "amt": -1000, "allow": True}  # 실 원장액·음수·부채 허용
    assert grant.revoked_at is not None and grant.clawback_hay_transaction_id == 7


async def test_rc_refund_clawback_idempotent(monkeypatch):
    """이미 회수(revoked_at NOT NULL) → 원장 재차감 없음 — 환불 웹훅 재수신 멱등."""
    pay = SimpleNamespace(store_transaction_id="t-rc-1", user_id=UID_UUID,
                          order_id=None, subscription_id=SUB_ID, status="refunded")
    sub = _sub(status="revoked")
    grant = SimpleNamespace(id=uuid.uuid4(), revoked_at="2026-07-01T00:00:00Z",
                            clawback_hay_transaction_id=7, hay_transaction_id=99)

    async def _apply(*a, **k):
        raise AssertionError("이중 회수하면 안 됨")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession(exec_results=[[pay], [grant]], get_map={Subscription: sub})
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"))
    assert res.outcome == HANDLED
    assert grant.clawback_hay_transaction_id == 7  # 변화 없음


async def test_rc_refund_missing_grant_permanent_failure(monkeypatch):
    """환불인데 회수 대상 증정 자체가 없음 → 성공 no-op 금지: permanent_failure(관측).

    (구독 결제가 있는데 증정 원장이 없다는 건 이상 상황 — 조용히 성공 처리하면 은폐된다, §4·§11.2.)
    """
    pay = SimpleNamespace(store_transaction_id="t-rc-1", user_id=UID_UUID,
                          order_id=None, subscription_id=SUB_ID, status="paid")
    sub = _sub(status="active")

    async def _apply(*a, **k):
        raise AssertionError("증정 없이 회수하면 안 됨")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession(exec_results=[[pay], []], get_map={Subscription: sub})  # grant 없음
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"))
    assert res.outcome == PERMANENT_FAILURE


async def test_rc_refund_zero_ledger_amount_permanent_failure(monkeypatch):
    """증정 원장액이 없음/0/음수면 성공 no-op 금지 — permanent_failure(관측)."""
    pay = SimpleNamespace(store_transaction_id="t-rc-1", user_id=UID_UUID,
                          order_id=None, subscription_id=SUB_ID, status="paid")
    sub = _sub(status="active")
    grant = SimpleNamespace(id=uuid.uuid4(), revoked_at=None, clawback_hay_transaction_id=None,
                            hay_transaction_id=None)  # 원장 연결 없음 → 회수량 불명

    async def _apply(*a, **k):
        raise AssertionError("원장액 불명인데 회수하면 안 됨")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession(exec_results=[[pay], [grant]], get_map={Subscription: sub})
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"))
    assert res.outcome == PERMANENT_FAILURE


async def test_rc_refund_payment_missing_dependency(monkeypatch):
    """환불이 결제보다 먼저 도착(Payment 없음) → dependency_missing(pending 수렴)."""
    s = FakeSession(exec_results=[[]])  # _payment_by_tx 없음
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"))
    assert res.outcome == DEPENDENCY_MISSING


async def test_rc_refund_both_ids_rejected(monkeypatch):
    """Payment의 order_id·subscription_id 양쪽 설정(payments_target_ck OR 허점) → 실오류 failed."""
    pay = SimpleNamespace(store_transaction_id="t-rc-1", user_id=UID_UUID,
                          order_id=ORD_ID, subscription_id=SUB_ID, status="paid")
    s = FakeSession(exec_results=[[pay]])
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"))
    assert res.outcome == PERMANENT_FAILURE


# --- 팩 환불·복구(§4) ---
async def test_rc_refund_pack_clawback(monkeypatch):
    """건초팩 환불 → order iap_purchase 합계만큼 음수 회수 + payment·order refunded."""
    pay = SimpleNamespace(store_transaction_id="tx-hay-1", user_id=UID_UUID,
                          order_id=ORD_ID, subscription_id=None, status="paid")
    order = SimpleNamespace(id=ORD_ID, status="paid")
    clawed = {}

    async def _pack_amt(session, order_id):
        return 300

    async def _apply(session, uid, t, amt, **kw):
        clawed.update(t=t, amt=amt, allow=kw.get("allow_negative"), order=kw.get("order_id"))
        return SimpleNamespace(id=8)

    monkeypatch.setattr(subscription.payment, "pack_ledger_amount", _pack_amt)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession(exec_results=[[pay]], get_map={Order: order})
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT",
                     transaction_id="tx-hay-1"))
    assert res.outcome == HANDLED
    assert clawed == {"t": "refund_revoke", "amt": -300, "allow": True, "order": ORD_ID}
    assert pay.status == "refunded" and order.status == "refunded"


async def test_rc_refund_reversed_pack_restore(monkeypatch):
    """REFUND_REVERSED(팩) → 원 주문 iap 합계 양수 직접 복구 + payment·order 복구."""
    pay = SimpleNamespace(store_transaction_id="tx-hay-1", user_id=UID_UUID,
                          order_id=ORD_ID, subscription_id=None, status="refunded")
    order = SimpleNamespace(id=ORD_ID, status="refunded")
    restored = {}

    async def _restore(session, user_id, order_id):
        restored.update(uid=user_id, order=order_id)
        return 300

    monkeypatch.setattr(subscription.payment, "restore_pack", _restore)
    s = FakeSession(exec_results=[[pay]], get_map={Order: order})
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="REFUND_REVERSED", transaction_id="tx-hay-1"))
    assert res.outcome == HANDLED
    assert restored == {"uid": UID_UUID, "order": ORD_ID}
    assert pay.status == "paid" and order.status == "paid"


async def test_rc_refund_reversed_subscription_restore(monkeypatch):
    """REFUND_REVERSED(구독) → 반대부호 복구 + revoked_at 해제 + status/payment 복구."""
    pay = SimpleNamespace(store_transaction_id="t-rc-1", user_id=UID_UUID,
                          order_id=None, subscription_id=SUB_ID, status="refunded")
    sub = _sub(status="revoked")
    grant = SimpleNamespace(id=uuid.uuid4(), revoked_at="2026-07-01T00:00:00Z",
                            clawback_hay_transaction_id=7, hay_transaction_id=99)
    grant_tx = SimpleNamespace(amount=1000)
    added = {}

    async def _apply(session, uid, t, amt, **kw):
        added.update(t=t, amt=amt, allow=kw.get("allow_negative"))
        return SimpleNamespace(id=9)

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession(exec_results=[[pay], [grant]],
                    get_map={Subscription: sub, HayTransaction: grant_tx})
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="REFUND_REVERSED"))
    assert res.outcome == HANDLED
    assert added == {"t": "admin_adjustment", "amt": 1000, "allow": True}  # 반대부호 양수
    assert grant.revoked_at is None and grant.clawback_hay_transaction_id is None
    assert pay.status == "paid" and sub.status == "active"


async def test_rc_refund_reversed_missing_grant_permanent_failure(monkeypatch):
    """복구인데 증정 자체가 없음 → 성공 no-op 금지: permanent_failure(관측)."""
    pay = SimpleNamespace(store_transaction_id="t-rc-1", user_id=UID_UUID,
                          order_id=None, subscription_id=SUB_ID, status="refunded")
    sub = _sub(status="revoked")

    async def _apply(*a, **k):
        raise AssertionError("증정 없이 복구하면 안 됨")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession(exec_results=[[pay], []], get_map={Subscription: sub})  # grant 없음
    res = await subscription.handle_revenuecat_event(s, _rc_event(type="REFUND_REVERSED"))
    assert res.outcome == PERMANENT_FAILURE


async def test_rc_refund_reversed_not_clawed_dependency(monkeypatch):
    """복구가 환불보다 먼저 도착(grant.revoked_at 없음) → dependency_missing(다음 틱 수렴).

    이전 코드는 NO_OP로 조용히 처리해 복구를 흘렸다 — 이제 pending으로 두어 회수 후 재수렴.
    """
    pay = SimpleNamespace(store_transaction_id="t-rc-1", user_id=UID_UUID,
                          order_id=None, subscription_id=SUB_ID, status="refunded")
    sub = _sub(status="revoked")
    grant = SimpleNamespace(id=uuid.uuid4(), revoked_at=None,  # 아직 회수 안 됨
                            clawback_hay_transaction_id=None, hay_transaction_id=99)

    async def _apply(*a, **k):
        raise AssertionError("회수 이력 없는데 복구하면 안 됨")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession(exec_results=[[pay], [grant]], get_map={Subscription: sub})
    res = await subscription.handle_revenuecat_event(s, _rc_event(type="REFUND_REVERSED"))
    assert res.outcome == DEPENDENCY_MISSING


# --- CUSTOMER_SUPPORT만 회수 / 자동갱신 해제 ---
async def test_rc_cancellation_unsubscribe_only_autorenew_off(monkeypatch):
    sub = _sub(status="active", auto_renew_enabled=True)

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    res = await subscription.handle_revenuecat_event(
        FakeSession(), _rc_event(type="CANCELLATION", cancel_reason="UNSUBSCRIBE"))
    assert res.outcome == HANDLED
    assert sub.auto_renew_enabled is False and sub.status == "active"  # 만료 전까지 혜택 유지


# --- 만료·유예·상태 단조(§3) ---
async def test_rc_expiration_marks_expired(monkeypatch):
    sub = _sub(status="active", expires_at=None, last_event_at=None)

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    res = await subscription.handle_revenuecat_event(FakeSession(), _rc_event(type="EXPIRATION"))
    assert res.outcome == HANDLED and sub.status == "expired"


async def test_rc_old_expiration_preserves_active(monkeypatch):
    """옛 EXPIRATION(event_ts <= last_event_at)은 활성 구독을 되돌리지 않는다(큰문제1 차단)."""
    from datetime import datetime, timezone
    sub = _sub(status="active", expires_at=None,
               last_event_at=datetime(2030, 1, 1, tzinfo=timezone.utc))

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    # event_timestamp_ms(2027)가 last_event_at(2030)보다 과거 → 상태 미적용.
    res = await subscription.handle_revenuecat_event(
        FakeSession(), _rc_event(type="EXPIRATION", event_timestamp_ms=1_800_000_000_000))
    assert res.outcome == NO_OP and sub.status == "active"


async def test_rc_expiration_older_expires_ignored(monkeypatch):
    """EXPIRATION 이중: expires < sub.expires_at 이면 옛 만료 — 무시."""
    from datetime import datetime, timezone
    sub = _sub(status="active", expires_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
               last_event_at=None)

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    res = await subscription.handle_revenuecat_event(
        FakeSession(), _rc_event(type="EXPIRATION", expiration_at_ms=1_700_000_000_000))
    assert res.outcome == NO_OP and sub.status == "active"


async def test_rc_expiration_no_subscription_dependency(monkeypatch):
    async def _by(session, otx, lock=False):
        return None

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    res = await subscription.handle_revenuecat_event(FakeSession(), _rc_event(type="EXPIRATION"))
    assert res.outcome == DEPENDENCY_MISSING


async def test_rc_billing_issue_grace(monkeypatch):
    sub = _sub(status="active", last_event_at=None)

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    res = await subscription.handle_revenuecat_event(FakeSession(), _rc_event(type="BILLING_ISSUE"))
    assert res.outcome == HANDLED and sub.status == "grace_period"


async def test_rc_monotonic_extension_exception(monkeypatch):
    """단조 연장 예외: 옛 event_ts라도 RENEWAL이 만료를 앞으로 연장하면 적용(backfill 지연 갱신 손실 방지)."""
    from datetime import datetime, timezone
    sub = _sub(status="active", expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
               last_event_at=datetime(2030, 6, 1, tzinfo=timezone.utc))
    same_pay = SimpleNamespace(user_id=UID_UUID, order_id=None, subscription_id=SUB_ID)

    async def _by(session, otx, lock=False):
        return sub

    async def _grant(session, uid, plan):
        return True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    # event_ts(2027) < last_event_at(2030-06)이지만 expiration_at_ms(2030-04)가 sub.expires(2030-01) 초과.
    res = await subscription.handle_revenuecat_event(
        FakeSession(exec_results=[[same_pay]]),  # 결제 멱등(같은 유저·구독)
        _rc_event(type="RENEWAL", event_timestamp_ms=1_800_000_000_000,
                  expiration_at_ms=1_900_000_000_000))
    assert res.outcome == HANDLED
    assert sub.expires_at == datetime.fromtimestamp(1_900_000_000, tz=timezone.utc)  # 앞 연장 적용


async def test_rc_extension_exception_forbidden_on_revoked(monkeypatch):
    """revoked(환불됨) 구독은 옛 RENEWAL의 큰 expires로 재활성화되지 않는다(연장 예외 금지)."""
    from datetime import datetime, timezone
    sub = _sub(status="revoked", expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
               last_event_at=datetime(2030, 6, 1, tzinfo=timezone.utc))
    same_pay = SimpleNamespace(user_id=UID_UUID, order_id=None, subscription_id=SUB_ID)

    async def _by(session, otx, lock=False):
        return sub

    async def _grant(session, uid, plan):
        return True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    res = await subscription.handle_revenuecat_event(
        FakeSession(exec_results=[[same_pay]]),  # 결제 멱등(같은 유저·구독)
        _rc_event(type="RENEWAL", event_timestamp_ms=1_800_000_000_000,
                  expiration_at_ms=1_900_000_000_000))
    assert res.outcome == HANDLED and sub.status == "revoked"  # 재활성화 안 됨


async def test_rc_product_change_no_state_or_plan_change(monkeypatch):
    """PRODUCT_CHANGE는 정보성 — 상태·plan·결제 무변경(누락 Payment 위조·요금제 오변경 방지)."""
    sub = _sub(plan="monthly", status="active", last_event_at=None)

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    s = FakeSession(exec_results=[[]])  # Apple 상품(config 미조회)
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="PRODUCT_CHANGE", product_id="app.moly.sub.monthly",
                     new_product_id="app.moly.sub.yearly"))
    assert res.outcome == HANDLED
    assert sub.plan == "monthly"  # 미변경
    assert not any(getattr(o, "store_transaction_id", None) for o in s.added)  # 결제 기록 없음


async def test_rc_non_renewing_grants_hay(monkeypatch):
    called = {}

    async def _grant(session, uid, product_id, transaction_id, *, store, amount=None, currency=None):
        called.update(pid=product_id, tx=transaction_id, store=store, amount=amount, cur=currency)
        return True  # grant_pack: 지급 성공(§11.3 — bool 반환)

    monkeypatch.setattr(payment, "grant_pack", _grant)
    res = await subscription.handle_revenuecat_event(
        FakeSession(),
        _rc_event(type="NON_RENEWING_PURCHASE", store="PLAY_STORE",
                  product_id="com.geniusjun.moly.hay.300", transaction_id="tx-hay-1",
                  price_in_purchased_currency=1.09, currency="USD"),
    )
    assert res.outcome == HANDLED
    assert called["pid"] == "com.geniusjun.moly.hay.300" and called["tx"] == "tx-hay-1"
    assert called["store"] == "play_store" and called["amount"] == Decimal("1.09")


async def test_rc_non_renewing_unregistered_product_permanent_failure(monkeypatch):
    """미등록 건초 상품 → grant_pack False → permanent_failure(건초 미지급이 관측서 빠지지 않게)."""
    async def _grant(session, uid, product_id, transaction_id, *, store, amount=None, currency=None):
        return False  # 미등록 상품

    monkeypatch.setattr(payment, "grant_pack", _grant)
    res = await subscription.handle_revenuecat_event(
        FakeSession(),
        _rc_event(type="NON_RENEWING_PURCHASE", store="APP_STORE",
                  product_id="com.unknown.pack", transaction_id="tx-hay-x"),
    )
    assert res.outcome == PERMANENT_FAILURE


async def test_rc_non_renewing_missing_transaction_id_permanent_failure(monkeypatch):
    """건초 IAP 거래ID 누락 → grant_pack 호출 전 permanent_failure(누락 IAP 은폐 금지)."""
    async def _grant(*a, **k):
        raise AssertionError("식별자 누락이면 grant_pack 호출 안 함")

    monkeypatch.setattr(payment, "grant_pack", _grant)
    res = await subscription.handle_revenuecat_event(
        FakeSession(),
        _rc_event(type="NON_RENEWING_PURCHASE",
                  product_id="com.geniusjun.moly.hay.300", transaction_id=""),
    )
    assert res.outcome == PERMANENT_FAILURE


# --- 결제 기록 범위(§3): INITIAL_PURCHASE/RENEWAL만 ---
async def test_rc_uncancellation_no_payment_record(monkeypatch):
    """UNCANCELLATION은 새 과금이 아니므로 결제 기록·증정을 만들지 않는다(위조 Payment 방지)."""
    sub = _sub(status="active", last_event_at=None)

    async def _by(session, otx, lock=False):
        return sub

    async def _pe(*a, **k):
        raise AssertionError("UNCANCELLATION은 결제 기록 안 함")

    async def _grant(*a, **k):
        raise AssertionError("UNCANCELLATION은 증정 안 함")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription.payment, "payment_exists", _pe)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    res = await subscription.handle_revenuecat_event(FakeSession(), _rc_event(type="UNCANCELLATION"))
    assert res.outcome == HANDLED and sub.auto_renew_enabled is True


# --- UNIQUE 충돌 후 의미 재검증(§6): 다른 유저/거래면 멱등 아님 → permanent_failure ---
async def test_rc_concurrent_create_other_user_permanent(monkeypatch):
    """동시 생성 UNIQUE 충돌 후 기존 구독이 다른 유저 소유면 멱등 수렴이 아니라 permanent_failure."""
    from sqlalchemy.exc import IntegrityError
    calls = {"n": 0}
    other = _sub(user_id=uuid.uuid4(), status="active")

    async def _by(session, otx, lock=False):
        calls["n"] += 1
        return None if calls["n"] == 1 else other  # 최초 None(생성 시도) → 충돌 후 기존행(다른 유저)

    monkeypatch.setattr(subscription, "_by_original_tx", _by)

    class _S(FakeSession):
        async def flush(self):
            raise IntegrityError("s", {}, _FakeOrig("23505",
                                 "subscriptions_original_transaction_id_key"))

    res = await subscription.handle_revenuecat_event(_S(), _rc_event())
    assert res.outcome == PERMANENT_FAILURE


async def test_record_subscription_payment_precheck_reuse_permanent():
    """사전 재조회 의미검증: transaction_id를 다른 유저가 재사용 → INSERT 전에 permanent_failure.

    (payment_exists 조기반환이 우회하던 경로 — 이제 결제 없이 증정으로 넘어가지 못한다, §6.)
    """
    sub = _sub(status="active")  # id=SUB_ID, user_id=UID_UUID
    other_pay = SimpleNamespace(user_id=uuid.uuid4(), order_id=None, subscription_id=uuid.uuid4())
    s = FakeSession(exec_results=[[other_pay]])  # _payment_by_tx 사전 재조회 → 다른 유저 결제
    with pytest.raises(subscription._HandlerSignal) as e:
        await subscription._record_subscription_payment(s, sub, _rc_event())
    assert e.value.outcome == PERMANENT_FAILURE
    assert not any(getattr(o, "store_transaction_id", None) for o in s.added)  # 결제 미기록


async def test_record_subscription_payment_precheck_same_owner_idempotent():
    """사전 재조회에서 같은 유저·구독 결제가 이미 있으면 멱등 no-op(재기록 없음)."""
    sub = _sub(status="active")
    same_pay = SimpleNamespace(user_id=UID_UUID, order_id=None, subscription_id=SUB_ID)
    s = FakeSession(exec_results=[[same_pay]])
    await subscription._record_subscription_payment(s, sub, _rc_event())  # 예외 없이 멱등
    assert not any(getattr(o, "store_transaction_id", None) for o in s.added)


async def test_record_subscription_payment_precheck_both_targets_permanent():
    """기존 결제가 order_id·subscription_id 둘 다 set(손상행)이면 XOR 위반 → permanent_failure(§6).

    DB CHECK는 OR라 둘 다 set을 못 막는다 — 폴백 조회가 다른 거래를 오환불할 위험을 런타임 XOR로 차단.
    """
    sub = _sub(status="active")
    corrupt = SimpleNamespace(user_id=UID_UUID, order_id=ORD_ID, subscription_id=SUB_ID)
    s = FakeSession(exec_results=[[corrupt]])
    with pytest.raises(subscription._HandlerSignal) as e:
        await subscription._record_subscription_payment(s, sub, _rc_event())
    assert e.value.outcome == PERMANENT_FAILURE
    assert not any(getattr(o, "store_transaction_id", None) for o in s.added)


async def test_record_subscription_payment_tx_reuse_permanent():
    """결제 UNIQUE 충돌(경쟁) 후 기존 결제가 다른 유저/구독이면 permanent_failure(오환불·위조 방지)."""
    from sqlalchemy.exc import IntegrityError
    sub = _sub(status="active")  # id=SUB_ID, user_id=UID_UUID
    other_pay = SimpleNamespace(user_id=uuid.uuid4(), order_id=None, subscription_id=uuid.uuid4())

    class _S(FakeSession):
        async def flush(self):
            raise IntegrityError("s", {}, _FakeOrig("23505",
                                 "payments_store_transaction_id_key"))

    # [0] 사전 재조회 None(경쟁 충돌 경로 유도) → INSERT 충돌 → [1] 재조회 다른 유저 결제
    s = _S(exec_results=[[], [other_pay]])
    with pytest.raises(subscription._HandlerSignal) as e:
        await subscription._record_subscription_payment(s, sub, _rc_event())
    assert e.value.outcome == PERMANENT_FAILURE


async def test_record_subscription_payment_tx_reuse_same_owner_idempotent():
    """결제 UNIQUE 충돌(경쟁)이지만 기존 결제가 같은 유저·구독이면 멱등(no raise)."""
    from sqlalchemy.exc import IntegrityError
    sub = _sub(status="active")
    same_pay = SimpleNamespace(user_id=UID_UUID, order_id=None, subscription_id=SUB_ID)

    class _S(FakeSession):
        async def flush(self):
            raise IntegrityError("s", {}, _FakeOrig("23505",
                                 "payments_store_transaction_id_key"))

    s = _S(exec_results=[[], [same_pay]])  # 사전 재조회 None → 충돌 → 재조회 같은 유저
    await subscription._record_subscription_payment(s, sub, _rc_event())  # 예외 없이 멱등 반환


# --- TRANSFER 전면 수동(§5): failed + 경제/구독/결제 무변경 ---
async def test_rc_transfer_manual_no_change():
    s = FakeSession()
    res = await subscription.handle_revenuecat_event(
        s, _rc_event(type="TRANSFER", transferred_from=["a"], transferred_to=["b"]))
    assert res.outcome == TRANSFER_MANUAL
    assert s.added == [] and s.committed is False and s.rolled_back is False


# --- Google Play 구독 매핑(SOMA-341) ---
def _cfg_row(mapping):
    return [SimpleNamespace(key="google_play_subscription_products", value=mapping)]


async def test_rc_google_subscription_maps_plan(monkeypatch):
    async def _by(session, otx, lock=False):
        return None

    async def _pe(session, tx):
        return True

    async def _grant(session, uid, plan):
        return True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription.payment, "payment_exists", _pe)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    s = FakeSession(exec_results=[_cfg_row({"moly_sub_yearly:yearly": "yearly"})])
    res = await subscription.handle_revenuecat_event(s, _rc_event(product_id="moly_sub_yearly:yearly"))
    sub_added = next(o for o in s.added if getattr(o, "status", None) == "active")
    assert sub_added.plan == "yearly" and res.outcome == HANDLED


async def test_rc_google_base_plan_suffix_normalized(monkeypatch):
    async def _by(session, otx, lock=False):
        return None

    async def _pe(session, tx):
        return True

    async def _grant(session, uid, plan):
        return True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription.payment, "payment_exists", _pe)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    s = FakeSession(exec_results=[_cfg_row({"moly_sub_monthly": "monthly"})])
    await subscription.handle_revenuecat_event(
        s, _rc_event(product_id="moly_sub_monthly:monthly-autorenew"))
    assert next(o for o in s.added if getattr(o, "status", None) == "active").plan == "monthly"


async def test_rc_unmapped_product_permanent_failure(monkeypatch):
    """매핑에 없는 상품 → permanent_failure(구독 생성·증정 없음, App Store 경로 무영향)."""
    called = {}

    async def _by(session, otx, lock=False):
        called["by"] = True
        return None

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    s = FakeSession(exec_results=[_cfg_row({})])
    res = await subscription.handle_revenuecat_event(s, _rc_event(product_id="unknown.product.x"))
    assert res.outcome == PERMANENT_FAILURE and s.added == [] and "by" not in called


async def test_rc_invalid_config_plan_value_permanent_failure(monkeypatch):
    async def _by(session, otx, lock=False):
        raise AssertionError("미등록 처리로 걸러져야 함")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    s = FakeSession(exec_results=[_cfg_row({"moly_sub_monthly": "montly"})])  # 오타값
    res = await subscription.handle_revenuecat_event(s, _rc_event(product_id="moly_sub_monthly"))
    assert res.outcome == PERMANENT_FAILURE and s.added == []


# --- 결제 원장 스토어·통화·금액 무손실(SOMA-343) ---
async def test_rc_payment_records_store_currency_lossless(monkeypatch):
    async def _by(session, otx, lock=False):
        return None

    async def _pe(session, tx):
        return False

    async def _grant(session, uid, plan):
        return True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription.payment, "payment_exists", _pe)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    s = FakeSession(exec_results=[_cfg_row({"moly_sub_monthly": "monthly"})])
    await subscription.handle_revenuecat_event(
        s, _rc_event(product_id="moly_sub_monthly", store="PLAY_STORE",
                     price_in_purchased_currency=4.99, currency="USD"))
    pay = next(o for o in s.added if getattr(o, "store_transaction_id", None) == "t-rc-1")
    assert pay.store == "play_store" and pay.currency == "USD"
    assert pay.amount == Decimal("4.99")


async def test_rc_payment_missing_currency_stored_null(monkeypatch):
    async def _by(session, otx, lock=False):
        return None

    async def _pe(session, tx):
        return False

    async def _grant(session, uid, plan):
        return True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription.payment, "payment_exists", _pe)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    s = FakeSession()
    ev = _rc_event(price_in_purchased_currency=4990)
    ev.pop("currency", None)
    await subscription.handle_revenuecat_event(s, ev)
    pay = next(o for o in s.added if getattr(o, "store_transaction_id", None) == "t-rc-1")
    assert pay.currency is None and pay.store == "app_store"


async def test_rc_bad_app_user_id_permanent_failure():
    s = FakeSession()
    res = await subscription.handle_revenuecat_event(s, _rc_event(app_user_id="not-a-uuid"))
    assert res.outcome == PERMANENT_FAILURE and s.added == []


async def test_rc_oversize_transaction_id_permanent_failure(monkeypatch):
    """과대 거래ID(>_TXN_ID_MAX)는 truncate 없이 permanent_failure로 거절 — 저장(원문)과 환불·복구
    조회(원문)가 어긋나 기존 Payment를 못 찾는 회귀를 원천 차단(SOMA-372)."""
    async def _by(session, otx, lock=False):
        raise AssertionError("과대 거래ID는 조회 이전에 거절돼야 함")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    huge = "x" * (subscription._TXN_ID_MAX + 1)
    res = await subscription.handle_revenuecat_event(
        FakeSession(), _rc_event(original_transaction_id=huge))
    assert res.outcome == PERMANENT_FAILURE


@pytest.mark.parametrize("over", [
    {"type": "CANCELLATION", "cancel_reason": "UNSUBSCRIBE",
     "original_transaction_id": "x" * (subscription._TXN_ID_MAX + 1)},   # 자동갱신 해제 조회
    {"type": "CANCELLATION", "cancel_reason": "CUSTOMER_SUPPORT",
     "transaction_id": "x" * (subscription._TXN_ID_MAX + 1)},            # 환불 회수 조회
    {"type": "REFUND_REVERSED",
     "transaction_id": "x" * (subscription._TXN_ID_MAX + 1)},            # 환불 취소 복구 조회
    {"type": "EXPIRATION",
     "original_transaction_id": "x" * (subscription._TXN_ID_MAX + 1)},   # 만료 조회
    {"type": "BILLING_ISSUE",
     "original_transaction_id": "x" * (subscription._TXN_ID_MAX + 1)},   # 유예 조회
])
async def test_rc_oversize_transaction_id_lookup_paths_rejected(monkeypatch, over):
    """조회 경로(해지·환불·환불취소·만료·유예)도 과대 거래ID(>_TXN_ID_MAX)를 조회 이전에
    permanent_failure로 거절 — 저장(원문)과 조회(원문) 정규화 통일(SOMA-372). 조회 함수가 불리면 실패."""
    async def _guard(*a, **k):
        raise AssertionError("과대 거래ID는 조회 이전에 거절돼야 함")

    monkeypatch.setattr(subscription, "_by_original_tx", _guard)
    monkeypatch.setattr(subscription, "_payment_by_tx", _guard)
    res = await subscription.handle_revenuecat_event(FakeSession(), _rc_event(**over))
    assert res.outcome == PERMANENT_FAILURE


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


@pytest.mark.parametrize("body", [
    {},                                          # 전부 없음
    {"event": {"type": "TEST"}},                 # api_version 없음(RC는 항상 포함)
    {"api_version": "1.0"},                      # event 없음
    {"api_version": "1.0", "event": "not-an-object"},
    {"api_version": "1.0", "event": {}},         # event.type 없음
    {"api_version": "1.0", "event": {"type": ""}},
    [1, 2],                                      # object가 아닌 JSON
])
def test_rc_webhook_malformed_body_422(monkeypatch, body):
    """인증 통과 후 top-level 형태 위반은 422 — RC가 실패로 기록·재시도해 가시화."""
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "revenuecat_webhook_auth", "sekret")

    async def _sess():
        yield None

    app.dependency_overrides[get_session] = _sess
    try:
        r = TestClient(app).post("/webhooks/revenuecat", json=body,
                                 headers={"Authorization": "sekret"})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"


def test_rc_webhook_broken_json_after_auth_422(monkeypatch):
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "revenuecat_webhook_auth", "sekret")

    async def _sess():
        yield None

    app.dependency_overrides[get_session] = _sess
    try:
        r = TestClient(app).post(
            "/webhooks/revenuecat", content=b"not-json",
            headers={"Authorization": "sekret", "Content-Type": "application/json"},
        )
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"


def test_rc_webhook_auth_precedes_body_parsing(monkeypatch):
    """깨진 JSON이라도 미인증이면 401 — body는 인증 후에만 파싱(fail-closed)."""
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "revenuecat_webhook_auth", "sekret")

    async def _sess():
        yield None

    app.dependency_overrides[get_session] = _sess
    try:
        r = TestClient(app).post(
            "/webhooks/revenuecat", content=b"not-json",
            headers={"Authorization": "wrong", "Content-Type": "application/json"},
        )
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


def test_rc_webhook_auth_ok_calls_ingest(monkeypatch):
    """인증 통과 → event.id로 inbox ingest 호출(내구 경계). 처리 성패와 무관히 200."""
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "revenuecat_webhook_auth", "sekret")
    called = {}

    async def _spy(session, event_id, event):
        called["event_id"] = event_id
        called["event"] = event
        return "handled"

    async def _sess():
        yield None

    monkeypatch.setattr(subscription, "ingest_event", _spy)
    app.dependency_overrides[get_session] = _sess
    try:
        r = TestClient(app).post(
            "/webhooks/revenuecat",
            json={"api_version": "1.0", "event": {"type": "TEST", "id": "evt-9"}},
            headers={"Authorization": "sekret"},
        )
        assert r.status_code == 200 and called["event_id"] == "evt-9"
        assert called["event"]["type"] == "TEST"
    finally:
        app.dependency_overrides.clear()


def test_rc_webhook_missing_event_id_422(monkeypatch):
    """event.id 누락(RC 계약 위반) → 422로 거절해 RC가 실패로 기록·재시도하게 한다."""
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "revenuecat_webhook_auth", "sekret")

    async def _sess():
        yield None

    app.dependency_overrides[get_session] = _sess
    try:
        r = TestClient(app).post(
            "/webhooks/revenuecat",
            json={"api_version": "1.0", "event": {"type": "INITIAL_PURCHASE"}},
            headers={"Authorization": "sekret"},
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"
    finally:
        app.dependency_overrides.clear()


def test_rc_webhook_oversized_event_id_422(monkeypatch):
    """과대 event.id(>255자) → 422 — inbox PK(B-tree) INSERT 전 거절(내구 보호)."""
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "revenuecat_webhook_auth", "sekret")

    async def _sess():
        yield None

    app.dependency_overrides[get_session] = _sess
    try:
        r = TestClient(app).post(
            "/webhooks/revenuecat",
            json={"api_version": "1.0", "event": {"type": "TEST", "id": "x" * 256}},
            headers={"Authorization": "sekret"},
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"
    finally:
        app.dependency_overrides.clear()


def test_rc_webhook_oversized_payload_422(monkeypatch):
    """과대 payload(>256KB) → 파싱 전 422(inbox JSONB 내구 보호)."""
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "revenuecat_webhook_auth", "sekret")

    async def _sess():
        yield None

    app.dependency_overrides[get_session] = _sess
    try:
        big = {"api_version": "1.0",
               "event": {"type": "TEST", "id": "evt-1", "blob": "a" * (300 * 1024)}}
        r = TestClient(app).post("/webhooks/revenuecat", json=big,
                                 headers={"Authorization": "sekret"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"
    finally:
        app.dependency_overrides.clear()


# --- 런칭 무료 기간이 get_subscription에 일관 반영 ---
async def test_get_subscription_reflects_launch_period(monkeypatch):
    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, trial_ends_at=None)

    async def _latest(session, uid):
        return None

    async def _cfg(session):
        return {"free_launch_until": "2999-01-01T00:00:00+00:00"}  # 먼 미래 = 런칭 중

    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(subscription, "_latest_sub", _latest)
    monkeypatch.setattr(subscription, "effective_token_config", _cfg)
    out = await subscription.get_subscription(FakeSession(), UID)
    assert out["status"] == "none" and out["in_trial"] is True
    assert out["trial_ends_at"].startswith("2999")  # 런칭 종료일로 표시


async def test_get_subscription_after_launch_no_trial(monkeypatch):
    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, trial_ends_at=None)

    async def _latest(session, uid):
        return None

    async def _cfg(session):
        return {"free_launch_until": "2000-01-01T00:00:00+00:00"}  # 과거 = 런칭 종료

    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(subscription, "_latest_sub", _latest)
    monkeypatch.setattr(subscription, "effective_token_config", _cfg)
    out = await subscription.get_subscription(FakeSession(), UID)
    assert out["in_trial"] is False and out["trial_ends_at"] is None
