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


def test_plans_authenticated():
    app.dependency_overrides[get_current_user] = lambda: UID
    try:
        response = TestClient(app).get("/subscription/plans")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert {plan["period"] for plan in response.json()["plans"]} == {"monthly", "yearly"}


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
        "store": "APP_STORE",
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
        return SimpleNamespace(id=1, balance_after=1000)

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession()
    await subscription.handle_revenuecat_event(s, _rc_event(price_in_purchased_currency=5900,
                                                            currency="KRW"))
    assert any(getattr(o, "status", None) == "active" for o in s.added)  # 구독 생성
    assert granted["amt"] == 1000 and s.committed
    assert any(getattr(o, "hay_transaction_id", None) == 1 for o in s.added)  # 증정 원장 연결
    # 결제 기록(payments) — 매출 단일 소스(DB_REFACTOR §B.3)
    pay = next(o for o in s.added if getattr(o, "store_transaction_id", None) == "t-rc-1")
    assert pay.amount == 5900 and pay.status == "paid" and pay.subscription_id is not None


async def test_rc_second_time_no_grant(monkeypatch):
    async def _by(session, otx, lock=False):
        return SimpleNamespace(id=uuid.uuid4(), user_id=UID_UUID, plan="monthly", status="active",
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
    grant = SimpleNamespace(revoked_at=None, clawback_hay_transaction_id=None)
    clawed = {}

    async def _by(session, otx, lock=False):
        return sub

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, hay_balance=1000)

    async def _apply(session, uid, t, amt, **kw):
        clawed["amt"] = amt
        return SimpleNamespace(id=7)

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    # exec 1회차 = 증정 이력(grant) 조회 — revoked_at NULL(미회수)
    await subscription.handle_revenuecat_event(
        FakeSession(exec_results=[[grant]]),
        _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"),
    )
    assert sub.status == "revoked" and clawed["amt"] == -1000  # min(1000 증정, 1000 잔액) 회수
    assert grant.revoked_at is not None  # 회수 완료 표식(멱등 키)
    assert grant.clawback_hay_transaction_id == 7  # 회수 원장 연결


async def test_rc_refund_clawback_idempotent(monkeypatch):
    """이미 회수(revoked_at NOT NULL) → 원장 재차감 없음 — 환불 웹훅 재수신 멱등."""
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="revoked")
    grant = SimpleNamespace(revoked_at="2026-07-01T00:00:00Z", clawback_hay_transaction_id=7)

    async def _by(session, otx, lock=False):
        return sub

    async def _apply(*a, **k):
        raise AssertionError("이중 회수하면 안 됨")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    await subscription.handle_revenuecat_event(
        FakeSession(exec_results=[[grant]]),
        _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"),
    )
    assert grant.clawback_hay_transaction_id == 7  # 변화 없음


async def test_rc_refund_no_grant_no_clawback(monkeypatch):
    """증정 이력이 없으면 회수할 것도 없음 — 받은 적 없는 건초를 뺏지 않는다."""
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active")

    async def _by(session, otx, lock=False):
        return sub

    async def _apply(*a, **k):
        raise AssertionError("증정 없이 회수하면 안 됨")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    await subscription.handle_revenuecat_event(
        FakeSession(exec_results=[[]]),  # grant 조회 결과 없음
        _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"),
    )
    assert sub.status == "revoked"


async def test_rc_refund_zero_balance_marks_revoked_without_ledger(monkeypatch):
    """잔액 0이면 원장 기록 없이 회수 표식만 — amount≠0 CHECK 위반 방지."""
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active")
    grant = SimpleNamespace(revoked_at=None, clawback_hay_transaction_id=None)

    async def _by(session, otx, lock=False):
        return sub

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, hay_balance=0)

    async def _apply(*a, **k):
        raise AssertionError("잔액 0인데 원장 기록하면 안 됨")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    await subscription.handle_revenuecat_event(
        FakeSession(exec_results=[[grant]]),
        _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"),
    )
    assert grant.revoked_at is not None and grant.clawback_hay_transaction_id is None


async def test_rc_cancellation_unsubscribe_only_autorenew_off(monkeypatch):
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active", auto_renew_enabled=True)

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    await subscription.handle_revenuecat_event(
        FakeSession(), _rc_event(type="CANCELLATION", cancel_reason="UNSUBSCRIBE")
    )
    assert sub.auto_renew_enabled is False and sub.status == "active"  # 만료 전까지 혜택 유지


async def test_rc_expiration_does_not_mutate_owned_appearance(monkeypatch):
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active")

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    await subscription.handle_revenuecat_event(FakeSession(), _rc_event(type="EXPIRATION"))
    assert sub.status == "expired"


async def test_rc_billing_issue_grace(monkeypatch):
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active")

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    await subscription.handle_revenuecat_event(FakeSession(), _rc_event(type="BILLING_ISSUE"))
    assert sub.status == "grace_period"


async def test_rc_non_renewing_grants_hay(monkeypatch):
    called = {}

    async def _grant(session, uid, product_id, transaction_id, *, store):
        called["pid"], called["tx"], called["store"] = product_id, transaction_id, store

    monkeypatch.setattr(payment, "grant_pack", _grant)
    await subscription.handle_revenuecat_event(
        FakeSession(),
        _rc_event(type="NON_RENEWING_PURCHASE", store="PLAY_STORE",
                  product_id="com.geniusjun.moly.hay.300", transaction_id="tx-hay-1"),
    )
    assert called["pid"] == "com.geniusjun.moly.hay.300" and called["tx"] == "tx-hay-1"
    assert called["store"] == "play_store"  # 실제 스토어 전달(SOMA-343)


# --- Google Play 구독 매핑(SOMA-341) ---
def _cfg_row(mapping):
    """app_config google_play_subscription_products 조회 결과 행."""
    return [SimpleNamespace(key="google_play_subscription_products", value=mapping)]


async def test_rc_google_subscription_maps_plan(monkeypatch):
    """app_config에 Google 상품 매핑이 있으면 Google 구독 최초구매가 올바른 plan을 활성화."""
    async def _by(session, otx, lock=False):
        return None

    async def _grant(session, uid, plan):
        return True  # 증정 생략(이 테스트는 plan 매핑만 검증)

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    # exec 순서: [0] config 조회(Apple 미스로 트리거), [1] payment_exists
    s = FakeSession(exec_results=[_cfg_row({"moly_sub_yearly:yearly": "yearly"}), []])
    await subscription.handle_revenuecat_event(s, _rc_event(product_id="moly_sub_yearly:yearly"))
    sub_added = next(o for o in s.added if getattr(o, "status", None) == "active")
    assert sub_added.plan == "yearly" and s.committed


async def test_rc_google_base_plan_suffix_normalized(monkeypatch):
    """config엔 구독ID만 등록돼 있어도 '구독ID:basePlanId' 이벤트를 정상 인식."""
    async def _by(session, otx, lock=False):
        return None

    async def _grant(session, uid, plan):
        return True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    s = FakeSession(exec_results=[_cfg_row({"moly_sub_monthly": "monthly"}), []])
    await subscription.handle_revenuecat_event(
        s, _rc_event(product_id="moly_sub_monthly:monthly-autorenew")
    )
    assert next(o for o in s.added if getattr(o, "status", None) == "active").plan == "monthly"


async def test_rc_product_change_uses_new_product_id(monkeypatch):
    """월→연 상품변경은 기존 product_id가 아니라 new_product_id 기준으로 요금제 갱신."""
    sub = SimpleNamespace(
        id=uuid.uuid4(), user_id=UID_UUID, plan="monthly", status="active",
        expires_at=None, auto_renew_enabled=True, latest_transaction_id=None,
    )

    async def _by(session, otx, lock=False):
        return sub

    async def _grant(session, uid, plan):
        return True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    s = FakeSession(exec_results=[[]])  # Apple 상품이라 config 조회 없음, [0]=payment_exists
    await subscription.handle_revenuecat_event(
        s, _rc_event(type="PRODUCT_CHANGE", product_id="app.moly.sub.monthly",
                     new_product_id="app.moly.sub.yearly"),
    )
    assert sub.plan == "yearly"  # 변경 후 상품 반영


async def test_rc_unmapped_product_no_activation(monkeypatch):
    """매핑에 없는 상품은 구독 생성·증정 없이 스킵(관측만) — App Store 경로엔 영향 없음."""
    called = {}

    async def _by(session, otx, lock=False):
        called["by"] = True
        return None

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    s = FakeSession(exec_results=[_cfg_row({})])  # 빈 매핑 → 미등록
    await subscription.handle_revenuecat_event(s, _rc_event(product_id="unknown.product.x"))
    assert s.added == [] and s.committed is False and "by" not in called


async def test_rc_invalid_config_plan_value_no_activation(monkeypatch):
    """config에 유효하지 않은 plan 값(오타 등)이면 혜택 미지급 — HAY_GRANT KeyError로 웹훅 크래시 방지."""
    async def _by(session, otx, lock=False):
        raise AssertionError("미등록 처리로 걸러져야 함 — 여기 오면 안 됨")

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    s = FakeSession(exec_results=[_cfg_row({"moly_sub_monthly": "montly"})])  # 오타값
    await subscription.handle_revenuecat_event(s, _rc_event(product_id="moly_sub_monthly"))
    assert s.added == [] and s.committed is False


# --- 결제 원장 스토어·통화·금액·환불 정합성(SOMA-343) ---
async def test_rc_payment_records_store_currency_lossless(monkeypatch):
    """Google Play 구독: 실제 store·원통화·소수점 금액이 손실 없이 기록된다."""
    async def _by(session, otx, lock=False):
        return None

    async def _grant(session, uid, plan):
        return True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    s = FakeSession(exec_results=[_cfg_row({"moly_sub_monthly": "monthly"}), []])
    await subscription.handle_revenuecat_event(
        s, _rc_event(product_id="moly_sub_monthly", store="PLAY_STORE",
                     price_in_purchased_currency=4.99, currency="USD"),
    )
    pay = next(o for o in s.added if getattr(o, "store_transaction_id", None) == "t-rc-1")
    assert pay.store == "play_store" and pay.currency == "USD"
    assert pay.amount == Decimal("4.99")  # 4.99 → 5 반올림 안 됨(무손실)


async def test_rc_payment_missing_currency_stored_null(monkeypatch):
    """통화 정보가 없으면 KRW로 확정하지 않고 NULL로 저장한다."""
    async def _by(session, otx, lock=False):
        return None

    async def _grant(session, uid, plan):
        return True

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_grant_exists", _grant)
    s = FakeSession(exec_results=[[]])  # Apple 상품(config 미조회), payment_exists
    ev = _rc_event(price_in_purchased_currency=4990)
    ev.pop("currency", None)
    await subscription.handle_revenuecat_event(s, ev)
    pay = next(o for o in s.added if getattr(o, "store_transaction_id", None) == "t-rc-1")
    assert pay.currency is None and pay.store == "app_store"


async def test_rc_refund_marks_payment_refunded(monkeypatch):
    """환불 시 원거래 payments.status가 refunded로 갱신 — 회수 상태와 매출 원장 일치."""
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="active")
    grant = SimpleNamespace(revoked_at=None, clawback_hay_transaction_id=None)
    pay = SimpleNamespace(store_transaction_id="t-rc-1", status="paid")

    async def _by(session, otx, lock=False):
        return sub

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, hay_balance=1000)

    async def _apply(session, uid, t, amt, **kw):
        return SimpleNamespace(id=7)

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    monkeypatch.setattr(subscription, "_load_profile", _lp)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    # exec: [0] clawback grant 조회, [1] 환불 대상 payment 조회
    s = FakeSession(exec_results=[[grant], [pay]])
    await subscription.handle_revenuecat_event(
        s, _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"),
    )
    assert sub.status == "revoked" and pay.status == "refunded"


async def test_rc_refund_payment_already_refunded_idempotent(monkeypatch):
    """이미 refunded면 그대로 유지 — 환불 웹훅 재수신에도 멱등."""
    sub = SimpleNamespace(user_id=UID_UUID, plan="monthly", status="revoked")
    grant = SimpleNamespace(revoked_at="2026-07-01T00:00:00Z", clawback_hay_transaction_id=7)
    pay = SimpleNamespace(store_transaction_id="t-rc-1", status="refunded")

    async def _by(session, otx, lock=False):
        return sub

    monkeypatch.setattr(subscription, "_by_original_tx", _by)
    s = FakeSession(exec_results=[[grant], [pay]])
    await subscription.handle_revenuecat_event(
        s, _rc_event(type="CANCELLATION", cancel_reason="CUSTOMER_SUPPORT"),
    )
    assert pay.status == "refunded"  # 변화 없음


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
        r = TestClient(app).post("/webhooks/revenuecat",
                                 json={"api_version": "1.0", "event": {"type": "TEST"}},
                                 headers={"Authorization": "sekret"})
        assert r.status_code == 200 and called["event"] == {"type": "TEST"}
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
