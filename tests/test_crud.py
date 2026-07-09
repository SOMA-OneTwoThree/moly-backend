"""economy·routine·shop·review 핵심 로직 + 인증(DB·의존 mock)."""
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.main import app
from app.services import economy, hay_ledger, review, routine, shop

UID = "11111111-1111-1111-1111-111111111111"
UID_UUID = uuid.UUID(UID)


class _Scalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _Scalars(self._items)

    def scalar(self):
        return self._items[0] if self._items else 0


class FakeSession:
    def __init__(self, get_obj=None, exec_results=None):
        self.get_obj = get_obj
        self.exec_results = list(exec_results or [])
        self.added = []
        self.committed = False

    async def get(self, model, key, **kw):
        return self.get_obj

    async def execute(self, stmt):
        return _Result(self.exec_results.pop(0) if self.exec_results else [])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        pass


# --- 건초 원장 ---
async def test_ledger_grant():
    p = SimpleNamespace(hay_balance=100)
    s = FakeSession(get_obj=p)
    bal = await hay_ledger.apply(s, UID_UUID, "attendance", 10)
    assert bal == 110 and p.hay_balance == 110
    assert s.added[0].amount == 10 and s.added[0].balance_after == 110


async def test_ledger_deduct():
    p = SimpleNamespace(hay_balance=1000)
    bal = await hay_ledger.apply(FakeSession(get_obj=p), UID_UUID, "shop_purchase", -400)
    assert bal == 600


async def test_ledger_insufficient():
    p = SimpleNamespace(hay_balance=5)
    with pytest.raises(AppError) as e:
        await hay_ledger.apply(FakeSession(get_obj=p), UID_UUID, "shop_purchase", -10)
    assert e.value.code == "INSUFFICIENT_HAY"


# --- 충전소: 출석/루틴보상 ---
def _patch_profile(monkeypatch, mod, tz="Asia/Seoul"):
    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, timezone=tz, hay_balance=640)

    monkeypatch.setattr(mod, "_load_profile", _lp)


async def test_attendance_success(monkeypatch):
    _patch_profile(monkeypatch, economy)

    async def _daily(session, uid, ad):
        return SimpleNamespace(attendance_claimed_at=None)

    async def _apply(session, uid, t, amt, **kw):
        return 650

    monkeypatch.setattr(economy, "_daily", _daily)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    out = await economy.claim_attendance(FakeSession(), UID)
    assert out == {"granted": 10, "balance_after": 650}


async def test_attendance_already_claimed(monkeypatch):
    _patch_profile(monkeypatch, economy)

    async def _daily(session, uid, ad):
        return SimpleNamespace(attendance_claimed_at="2026-07-07T00:00:00Z")

    monkeypatch.setattr(economy, "_daily", _daily)
    with pytest.raises(AppError) as e:
        await economy.claim_attendance(FakeSession(), UID)
    assert e.value.code == "ALREADY_CLAIMED"


async def test_routine_reward_goal_not_met(monkeypatch):
    _patch_profile(monkeypatch, economy)

    async def _count(session, uid, ad):
        return 1  # < 2

    monkeypatch.setattr(economy, "_routine_completions_today", _count)
    with pytest.raises(AppError) as e:
        await economy.claim_routine_reward(FakeSession(), UID)
    assert e.value.code == "ROUTINE_GOAL_NOT_MET"


# --- 루틴 통계(streak) ---
async def test_routine_statistics_streak(monkeypatch):
    ad = date(2026, 7, 7)

    async def _today(session, user_id):
        return UID_UUID, ad

    async def _owned(session, uid, rid):
        return SimpleNamespace(id=uuid.uuid4(), frequency_per_week=3, days_of_week=None)

    monkeypatch.setattr(routine, "_today", _today)
    monkeypatch.setattr(routine, "_load_owned", _owned)
    # 오늘·어제·그제 연속 완료 → streak 3
    dates = [ad, ad - timedelta(days=1), ad - timedelta(days=2), ad - timedelta(days=10)]
    out = await routine.statistics(FakeSession(exec_results=[dates]), UID, str(uuid.uuid4()))
    assert out["streak"] == 3
    assert out["completed_today"] is True and out["target_count"] == 3 and out["days_of_week"] is None
    assert 0.0 <= out["completion_rate"] <= 1.0
    # 이번 주(월~일) 요일별 완료 여부·수행 횟수 — 실제 요일 기준으로 검증
    wk_start = ad - timedelta(days=ad.isoweekday() - 1)
    in_week = [d for d in dates if wk_start <= d <= wk_start + timedelta(days=6)]
    tw = out["this_week"]
    assert tw["completed_count"] == len(in_week)
    for d in in_week:
        assert tw["by_weekday"][str(d.isoweekday())] is True


async def test_routine_create_weekday_mode():
    s = FakeSession()
    req = SimpleNamespace(name="운동", frequency_per_week=None, days_of_week=[1, 3, 5],
                          reminder_enabled=False, reminder_time=None)
    out = await routine.create_routine(s, UID, req)
    assert out["days_of_week"] == [1, 3, 5]
    assert out["frequency_per_week"] == 3  # 요일 수로 파생
    assert s.added[0].days_of_week == [1, 3, 5] and s.added[0].frequency_per_week == 3


async def test_routine_update_mode_switch(monkeypatch):
    r = SimpleNamespace(name="x", frequency_per_week=2, days_of_week=None,
                        reminder_enabled=False, reminder_time=None)

    async def _owned(session, uid, rid):
        return r

    monkeypatch.setattr(routine, "_load_owned", _owned)
    # 주N회 → 요일별 전환: frequency 파생
    req = SimpleNamespace(name=None, frequency_per_week=None, days_of_week=[2, 4, 6, 7],
                          reminder_enabled=None, reminder_time=None)
    req.model_fields_set = {"days_of_week"}
    await routine.update_routine(FakeSession(), UID, str(uuid.uuid4()), req)
    assert r.days_of_week == [2, 4, 6, 7] and r.frequency_per_week == 4
    # 요일별 → 주N회 전환([] + frequency 동반)
    req2 = SimpleNamespace(name=None, frequency_per_week=3, days_of_week=[],
                           reminder_enabled=None, reminder_time=None)
    req2.model_fields_set = {"days_of_week", "frequency_per_week"}
    await routine.update_routine(FakeSession(), UID, str(uuid.uuid4()), req2)
    assert r.days_of_week is None and r.frequency_per_week == 3


# --- 상점 구매 ---
def _item(**over):
    base = dict(id=uuid.uuid4(), slot="head", name="모자", price_hay=1000,
               is_subscriber_only=False, is_active=True, assets={})
    base.update(over)
    return SimpleNamespace(**base)


async def test_purchase_subscriber_only_rejected(monkeypatch):
    async def _load(session, pid):
        return _item(is_subscriber_only=True, price_hay=None)

    monkeypatch.setattr(shop, "_load_item", _load)
    with pytest.raises(AppError) as e:
        await shop.purchase(FakeSession(), UID, "x")
    assert e.value.code == "SUBSCRIBER_ONLY"


async def test_purchase_already_owned(monkeypatch):
    it = _item()

    async def _load(session, pid):
        return it

    async def _owned(session, uid):
        return {it.id}

    monkeypatch.setattr(shop, "_load_item", _load)
    monkeypatch.setattr(shop, "_owned_ids", _owned)
    with pytest.raises(AppError) as e:
        await shop.purchase(FakeSession(), UID, "x")
    assert e.value.code == "ALREADY_OWNED"


async def test_purchase_success(monkeypatch):
    it = _item(price_hay=1000)

    async def _load(session, pid):
        return it

    async def _owned(session, uid):
        return set()

    async def _apply(session, uid, t, amt, **kw):
        assert amt == -1000
        return 640

    monkeypatch.setattr(shop, "_load_item", _load)
    monkeypatch.setattr(shop, "_owned_ids", _owned)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    session = FakeSession()
    out = await shop.purchase(session, UID, "x")
    assert out["price_hay"] == 1000 and out["balance_after"] == 640
    assert session.committed is True


# --- 리뷰 ---
async def test_review_marks_once(monkeypatch):
    profile = SimpleNamespace(review_prompted_at=None)

    async def _lp(session, user_id):
        return profile

    monkeypatch.setattr(review, "_load_profile", _lp)
    session = FakeSession()
    await review.mark_prompted(session, UID)
    assert profile.review_prompted_at is not None and session.committed is True


async def test_review_idempotent_when_already(monkeypatch):
    profile = SimpleNamespace(review_prompted_at="2026-01-01T00:00:00Z")

    async def _lp(session, user_id):
        return profile

    monkeypatch.setattr(review, "_load_profile", _lp)
    session = FakeSession()
    await review.mark_prompted(session, UID)
    assert session.committed is False


# --- 인증 ---
async def _dummy_session():
    yield None


@pytest.mark.parametrize("method,path", [
    ("get", "/wallet"),
    ("get", "/charging-station"),
    ("get", "/routines"),
    ("get", "/shop/products"),
    ("post", "/review/prompted"),
])
def test_crud_endpoints_require_auth(method, path):
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = getattr(TestClient(app), method)(path)
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
