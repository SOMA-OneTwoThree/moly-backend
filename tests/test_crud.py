"""economy·routine·shop·review 핵심 로직 + 인증(DB·의존 mock)."""
import uuid
from datetime import date, time, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.main import app
from app.schemas.routine import PatchRoutineRequest
from app.schemas.shop import EquipmentPutRequest, ShopProduct
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
        self.deleted = []
        self.committed = False
        self.get_calls = []

    async def get(self, model, key, **kw):
        self.get_calls.append((model, key, kw))
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

    async def delete(self, obj):
        self.deleted.append(obj)


# --- 건초 원장 ---
async def test_ledger_grant():
    p = SimpleNamespace(hay_balance=100)
    s = FakeSession(get_obj=p)
    tx = await hay_ledger.apply(s, UID_UUID, "attendance", 10)
    assert tx.balance_after == 110 and p.hay_balance == 110
    assert s.added[0].amount == 10 and s.added[0].balance_after == 110


async def test_ledger_deduct():
    p = SimpleNamespace(hay_balance=1000)
    tx = await hay_ledger.apply(FakeSession(get_obj=p), UID_UUID, "shop_purchase", -400)
    assert tx.balance_after == 600


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
        return SimpleNamespace(id=1, balance_after=650)

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
        return SimpleNamespace(id=uuid.uuid4(), frequency_per_week=3, days_of_week=[1, 3, 5])

    monkeypatch.setattr(routine, "_today", _today)
    monkeypatch.setattr(routine, "_load_owned", _owned)
    # 오늘·어제·그제 연속 완료 → streak 3
    dates = [ad, ad - timedelta(days=1), ad - timedelta(days=2), ad - timedelta(days=10)]
    out = await routine.statistics(FakeSession(exec_results=[dates]), UID, str(uuid.uuid4()))
    assert out["streak"] == 3
    assert out["completed_today"] is True and out["target_count"] == 3
    assert out["days_of_week"] == [1, 3, 5]
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
    req = SimpleNamespace(name="운동", days_of_week=[1, 3, 5],
                          reminder_enabled=False, reminder_time=None)
    out = await routine.create_routine(s, UID, req)
    assert out["days_of_week"] == [1, 3, 5]
    assert out["frequency_per_week"] == 3  # 요일 수로 파생
    assert s.added[0].days_of_week == [1, 3, 5] and s.added[0].frequency_per_week == 3


async def test_routine_update_days(monkeypatch):
    r = SimpleNamespace(name="x", frequency_per_week=3, days_of_week=[1, 3, 5],
                        reminder_enabled=False, reminder_time=None)

    async def _owned(session, uid, rid):
        return r

    monkeypatch.setattr(routine, "_load_owned", _owned)
    # 요일 변경: frequency 파생
    req = PatchRoutineRequest(days_of_week=[2, 4, 6, 7])
    await routine.update_routine(FakeSession(), UID, str(uuid.uuid4()), req)
    assert r.days_of_week == [2, 4, 6, 7] and r.frequency_per_week == 4
    # days_of_week 생략 시 스케줄 불변
    req2 = PatchRoutineRequest(name="이름만")
    await routine.update_routine(FakeSession(), UID, str(uuid.uuid4()), req2)
    assert r.name == "이름만" and r.days_of_week == [2, 4, 6, 7] and r.frequency_per_week == 4


async def test_routine_update_reminder_time_null_clears(monkeypatch):
    r = SimpleNamespace(name="x", frequency_per_week=3, days_of_week=[1, 3, 5],
                        reminder_enabled=True, reminder_time=time(9, 0))

    async def _owned(session, uid, rid):
        return r

    monkeypatch.setattr(routine, "_load_owned", _owned)
    # 필드 생략 → 기존 시간 유지
    await routine.update_routine(
        FakeSession(), UID, str(uuid.uuid4()),
        PatchRoutineRequest.model_validate({"reminder_enabled": False}),
    )
    assert r.reminder_enabled is False and r.reminder_time == time(9, 0)
    # 명시적 null → 제거
    await routine.update_routine(
        FakeSession(), UID, str(uuid.uuid4()),
        PatchRoutineRequest.model_validate({"reminder_time": None}),
    )
    assert r.reminder_time is None


def test_routine_request_weekday_only():
    from pydantic import ValidationError

    from app.schemas.routine import CreateRoutineRequest, PatchRoutineRequest

    # days_of_week 필수
    with pytest.raises(ValidationError):
        CreateRoutineRequest(name="운동")
    # frequency_per_week는 더 이상 받지 않음(extra forbid)
    with pytest.raises(ValidationError):
        CreateRoutineRequest(name="운동", days_of_week=[1], frequency_per_week=5)
    with pytest.raises(ValidationError):
        PatchRoutineRequest(frequency_per_week=5)
    # 빈 배열(구 주N회 전환 신호)은 거부
    with pytest.raises(ValidationError):
        PatchRoutineRequest(days_of_week=[])
    # 중복은 거부, 정상 입력은 정렬
    with pytest.raises(ValidationError):
        CreateRoutineRequest(name="운동", days_of_week=[5, 1, 1])
    assert CreateRoutineRequest(name="운동", days_of_week=[5, 1]).days_of_week == [1, 5]
    assert PatchRoutineRequest(days_of_week=[7, 2]).days_of_week == [2, 7]


# --- 상점 구매 ---
def _item(**over):
    public_id = over.pop("public_id", f"head_{uuid.uuid4().hex[:8]}")
    base = dict(
        id=uuid.uuid4(), public_id=public_id, slot="head", name="모자", price_hay=1000,
        is_subscriber_only=False, is_active=True, asset_version=1, sort_order=1,
        assets={
            "thumbnail_url": f"https://cdn.example.com/{public_id}/v1/thumb.png",
            "detail_url": f"https://cdn.example.com/{public_id}/v1/detail.png",
            "upright_layer_url": f"https://cdn.example.com/{public_id}/v1/upright.png",
        },
    )
    base.update(over)
    return SimpleNamespace(**base)


def _theme(**over):
    public_id = over.pop("public_id", "theme_default")
    base = dict(
        id=uuid.uuid4(), public_id=public_id, slot="theme", name="집", price_hay=None,
        is_subscriber_only=False, is_active=True, asset_version=1, sort_order=1,
        assets={
            "thumbnail_url": f"https://cdn.example.com/{public_id}/v1/thumb.png",
            "detail_url": f"https://cdn.example.com/{public_id}/v1/detail.png",
            "scene": {
                "canvas": {"width": 393, "height": 852},
                "character_frame": {"x": 51, "y": 338.8, "width": 171, "height": 85.2},
                "character_url": f"https://cdn.example.com/{public_id}/v1/character.png",
                "layers": [{
                    "id": "background",
                    "frame": {"x": 0, "y": 0, "width": 393, "height": 852},
                    "z_index": 0,
                    "day_url": f"https://cdn.example.com/{public_id}/v1/background.png",
                }],
            },
        },
    )
    base.update(over)
    return SimpleNamespace(**base)


async def test_purchase_non_purchasable_rejected(monkeypatch):
    """price_hay NULL = 비매품(기본 지급). 미보유 상태에서도 구매 대상이 아니다."""
    async def _load(session, pid):
        return _item(price_hay=None)

    async def _owned(session, uid):
        return set()

    monkeypatch.setattr(shop, "_load_item", _load)
    monkeypatch.setattr(shop, "_owned_ids", _owned)
    with pytest.raises(AppError) as e:
        await shop.purchase(FakeSession(), UID, "x")
    assert e.value.code == "VALIDATION"


async def test_purchase_zero_price_rejected(monkeypatch):
    """price_hay 0은 원장 CHECK(amount<>0) 위반 전에 422로 차단된다."""
    async def _load(session, pid):
        return _item(price_hay=0)

    async def _owned(session, uid):
        return set()

    monkeypatch.setattr(shop, "_load_item", _load)
    monkeypatch.setattr(shop, "_owned_ids", _owned)
    with pytest.raises(AppError) as e:
        await shop.purchase(FakeSession(), UID, "x")
    assert e.value.code == "VALIDATION"


async def test_purchase_already_owned(monkeypatch):
    it = _theme()

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
    applied = {}

    async def _load(session, pid):
        return it

    async def _owned(session, uid):
        return set()

    async def _apply(session, uid, t, amt, **kw):
        assert amt == -1000
        applied["order_id"] = kw.get("order_id")
        return SimpleNamespace(id=1, balance_after=640)

    monkeypatch.setattr(shop, "_load_item", _load)
    monkeypatch.setattr(shop, "_owned_ids", _owned)
    monkeypatch.setattr(hay_ledger, "apply", _apply)

    async def _lock(session, uid):
        pass

    monkeypatch.setattr(shop, "_lock_user", _lock)
    session = FakeSession()
    out = await shop.purchase(session, UID, "x", idempotency_key="purchase-key")
    assert out["product_id"] == it.public_id
    assert out["price_hay"] == 1000 and out["balance_after"] == 640
    # 주문 생성(HAY·paid) + 가격 스냅샷 + 원장·인벤토리가 주문으로 연결(DB_REFACTOR §B.2)
    order, order_item, user_item, idempotency = session.added
    assert order.currency == "HAY" and order.status == "paid" and order.total_amount == 1000
    assert order_item.order_id == order.id and order_item.product_id == it.id
    assert order_item.unit_price == 1000  # 구매 시점 가격 스냅샷
    assert user_item.source == "purchase" and user_item.order_id == order.id
    assert user_item.product_id == it.id
    assert applied["order_id"] == order.id  # 차감 원장 → 주문 연결
    assert out["order_id"] == str(order.id)
    assert idempotency.key == "shop-purchase:purchase-key"
    assert idempotency.response == out
    assert session.committed is True


async def test_purchase_replays_cached_response(monkeypatch):
    cached_response = {
        "product_id": "head_cap",
        "order_id": "11111111-1111-1111-1111-111111111111",
        "price_hay": 1000,
        "balance_after": 640,
    }
    session = FakeSession(get_obj=SimpleNamespace(response=cached_response))

    async def _must_not_load(session, product_id):
        raise AssertionError("cached purchase must not run again")

    monkeypatch.setattr(shop, "_load_item", _must_not_load)

    out = await shop.purchase(session, UID, "different-product", idempotency_key="same-key")

    assert out == cached_response
    assert session.get_calls[0][1] == (UID_UUID, "shop-purchase:same-key")
    assert session.committed is False


async def test_purchase_incompatible_cache_fails_closed(monkeypatch):
    async def _must_not_load(session, product_id):
        raise AssertionError("비호환 캐시를 새 구매로 재실행하면 안 됨")

    monkeypatch.setattr(shop, "_load_item", _must_not_load)
    session = FakeSession(get_obj=SimpleNamespace(response={"reply": {"content": "채팅 응답"}}))

    # 재시도해도 행은 보존된 채 매번 500 — 지우면 다음 재시도가 새 구매로 실행되어
    # 차감·지급이 중복된다. 정리는 운영 스크립트(--delete-invalid) 전용(api-inventory.md).
    for _ in range(2):
        with pytest.raises(AppError) as exc:
            await shop.purchase(session, UID, "x", idempotency_key="legacy-key")
        assert exc.value.code == "INTERNAL"
        assert exc.value.http_status == 500

    assert session.added == []
    assert session.deleted == []
    assert session.committed is False


# --- 장착(user_items 통합 — DB_REFACTOR §B.4) ---
def _row(product_id, source="purchase", equipped_slot=None):
    return SimpleNamespace(product_id=product_id, source=source,
                           equipped_slot=equipped_slot, equipped_at=None)


async def test_put_equipment_replace_unequips_previous(monkeypatch):
    """같은 슬롯 교체 = 기존 자동 해제(equipped_slot NULL) + 새 아이템 장착."""
    theme, a, b = _theme(), _item(slot="head"), _item(slot="head")
    theme_row = _row(theme.id, source="admin_grant", equipped_slot="theme")
    row_a, row_b = _row(a.id), _row(b.id, equipped_slot="head")
    products = {product.public_id: product for product in (theme, a, b)}

    async def _load(session, pid):
        return products[pid]

    monkeypatch.setattr(shop, "_load_item", _load)
    s = FakeSession(get_obj=SimpleNamespace(id=UID_UUID), exec_results=[[theme_row, row_a, row_b]])
    req = EquipmentPutRequest(
        theme_id=theme.public_id, head_id=a.public_id, neck_id=None, body_id=None
    )
    out = await shop.put_equipment(s, UID, req)
    assert row_b.equipped_slot is None  # 기존 장착 자동 해제(보유는 유지)
    assert row_a.equipped_slot == "head" and row_a.equipped_at is not None
    assert out["theme_id"] == "theme_default"
    assert out["head_id"] == a.public_id and s.committed is True
    assert s.get_calls[0][2] == {"with_for_update": True}


async def test_put_equipment_not_owned_rejected(monkeypatch):
    theme, it = _theme(), _item(slot="head")
    theme_row = _row(theme.id, source="admin_grant", equipped_slot="theme")
    products = {product.public_id: product for product in (theme, it)}

    async def _load(session, pid):
        return products[pid]

    monkeypatch.setattr(shop, "_load_item", _load)
    s = FakeSession(get_obj=SimpleNamespace(id=UID_UUID), exec_results=[[theme_row]])
    req = EquipmentPutRequest(
        theme_id=theme.public_id, head_id=it.public_id, neck_id=None, body_id=None
    )
    with pytest.raises(AppError) as e:
        await shop.put_equipment(s, UID, req)
    assert e.value.code == "NOT_OWNED"


async def test_put_equipment_slot_mismatch_rejected(monkeypatch):
    it = _item(slot="head")  # head 상품을 theme 슬롯에

    async def _load(session, pid):
        return it

    monkeypatch.setattr(shop, "_load_item", _load)
    s = FakeSession(get_obj=SimpleNamespace(id=UID_UUID), exec_results=[[_row(it.id)]])
    req = EquipmentPutRequest(theme_id=it.public_id, head_id=None, neck_id=None, body_id=None)
    with pytest.raises(AppError) as e:
        await shop.put_equipment(s, UID, req)
    assert e.value.code == "VALIDATION"


async def test_inventory_excludes_subscription_rows(monkeypatch):
    """인벤토리는 구독 장착 행을 제외하고 카탈로그와 같은 전체 DTO를 반환한다."""
    owned, subscription = _item(), _item()
    rows = [_row(owned.id), _row(subscription.id, source="subscription", equipped_slot="head")]
    out = await shop.get_inventory(FakeSession(exec_results=[rows, [owned]]), UID)
    assert [item["id"] for item in out["data"]] == [owned.public_id]
    assert out["data"][0]["owned"] is True
    assert "upright_layer_url" in out["data"][0]["assets"]


async def test_catalog_partitions_themes_and_items_with_consistent_flags():
    theme, head = _theme(), _item()
    rows = [
        _row(theme.id, source="admin_grant", equipped_slot="theme"),
        _row(head.id, source="admin_grant"),
    ]
    out = await shop.get_products(FakeSession(exec_results=[[theme, head], rows]), UID)
    assert [item["id"] for item in out["themes"]] == ["theme_default"]
    assert out["themes"][0]["owned"] is True and out["themes"][0]["equipped"] is True
    assert out["items"][0]["owned"] is True and out["items"][0]["equipped"] is False
    assert "backgrounds" not in out


async def test_get_equipment_uses_public_ids_and_requires_theme():
    theme, head = _theme(), _item(public_id="head_sunglasses")
    rows = [
        _row(theme.id, source="admin_grant", equipped_slot="theme"),
        _row(head.id, source="admin_grant", equipped_slot="head"),
    ]
    out = await shop.get_equipment(FakeSession(exec_results=[rows, [theme, head]]), UID)
    assert out == {
        "theme_id": "theme_default",
        "head_id": "head_sunglasses",
        "neck_id": None,
        "body_id": None,
    }

    with pytest.raises(AppError) as exc:
        await shop.get_equipment(FakeSession(exec_results=[[], []]), UID)
    assert exc.value.code == "INTERNAL"


def test_equipment_request_requires_non_null_theme_and_all_keys():
    with pytest.raises(ValueError):
        EquipmentPutRequest.model_validate(
            {"theme_id": None, "head_id": None, "neck_id": None, "body_id": None}
        )
    with pytest.raises(ValueError):
        EquipmentPutRequest.model_validate(
            {"theme_id": "theme_default", "head_id": None, "neck_id": None}
        )


def test_theme_scene_rejects_duplicate_layer_ids():
    theme = _theme()
    duplicate = dict(theme.assets)
    duplicate["scene"] = dict(duplicate["scene"])
    duplicate["scene"]["layers"] = duplicate["scene"]["layers"] * 2
    with pytest.raises(ValueError):
        ShopProduct(
            id=theme.public_id,
            name=theme.name,
            slot=theme.slot,
            price_hay=theme.price_hay,
            owned=True,
            equipped=True,
            asset_version=theme.asset_version,
            assets=duplicate,
        )


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
