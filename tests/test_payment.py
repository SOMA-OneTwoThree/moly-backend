"""건초 IAP(payment.grant_pack) — 주문+결제+원장 생성·멱등(DB mock)."""
import uuid
from types import SimpleNamespace

from app.services import hay_ledger, payment

UID_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")


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

    async def execute(self, stmt):
        return _Result(self.exec_results.pop(0) if self.exec_results else [])

    def add(self, obj):
        self.added.append(obj)


def _pack():
    return SimpleNamespace(id=uuid.uuid4(), product_type="hay_pack", price_krw=1500,
                           hay_amount=300, app_store_product_id="com.geniusjun.moly.hay.300")


async def test_grant_pack_creates_order_payment_ledger(monkeypatch):
    pack = _pack()
    applied = {}

    async def _apply(session, uid, t, amt, **kw):
        applied.update(type=t, amount=amt, order_id=kw.get("order_id"))
        return SimpleNamespace(id=1, balance_after=300)

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    # exec 1회차 = 결제 멱등 조회(없음), 2회차 = 상품 조회
    s = FakeSession(exec_results=[[], [pack]])
    await payment.grant_pack(s, UID_UUID, pack.app_store_product_id, "tx-1", store="app_store")
    order, order_item, pay = s.added
    assert order.currency == "KRW" and order.status == "paid" and order.total_amount == 1500
    assert order_item.order_id == order.id and order_item.unit_price == 1500
    assert order_item.product_id == pack.id
    assert applied == {"type": "iap_purchase", "amount": 300, "order_id": order.id}
    assert pay.order_id == order.id and pay.store_transaction_id == "tx-1"
    assert pay.amount == 1500 and pay.status == "paid"
    assert pay.store == "app_store"  # 실제 스토어 기록(SOMA-343): 인자로 받은 store 그대로


class _WhereCaptureSession(FakeSession):
    """상품 조회 WHERE 절을 기록 — select(Product)는 SELECT에 전 컬럼을 렌더하므로 WHERE만 검사."""
    def __init__(self, exec_results=None):
        super().__init__(exec_results)
        self.wheres = []

    async def execute(self, stmt):
        self.wheres.append(str(getattr(stmt, "whereclause", "")))
        return await super().execute(stmt)


async def test_grant_pack_play_store_looks_up_play_column(monkeypatch):
    """store=play_store면 play_store_product_id 컬럼으로 상품을 조회한다(SOMA-342)."""
    pack = _pack()

    async def _apply(session, uid, t, amt, **kw):
        return SimpleNamespace(id=1, balance_after=300)

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = _WhereCaptureSession(exec_results=[[], [pack]])  # [0] 멱등 조회, [1] 상품 조회
    await payment.grant_pack(s, UID_UUID, "moly_hay_300", "tx-9", store="play_store")
    assert "play_store_product_id" in s.wheres[1]  # 상품 조회 WHERE가 play 컬럼 사용
    assert s.added  # 정상 지급


async def test_grant_pack_app_store_looks_up_app_column(monkeypatch):
    """store=app_store(기본)면 app_store_product_id 컬럼으로 조회 — 기존 Apple 경로 무변경."""
    pack = _pack()

    async def _apply(session, uid, t, amt, **kw):
        return SimpleNamespace(id=1, balance_after=300)

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = _WhereCaptureSession(exec_results=[[], [pack]])
    await payment.grant_pack(s, UID_UUID, pack.app_store_product_id, "tx-10", store="app_store")
    assert "play_store_product_id" not in s.wheres[1]  # play 컬럼 미사용
    assert "app_store_product_id" in s.wheres[1]


async def test_grant_pack_idempotent_on_duplicate_transaction(monkeypatch):
    async def _apply(*a, **k):
        raise AssertionError("중복 거래에 재지급하면 안 됨")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession(exec_results=[[SimpleNamespace(id=uuid.uuid4())]])  # 결제 이미 존재
    await payment.grant_pack(s, UID_UUID, "com.geniusjun.moly.hay.300", "tx-1", store="app_store")
    assert s.added == []


async def test_grant_pack_unknown_product_skips(monkeypatch):
    async def _apply(*a, **k):
        raise AssertionError("미상 상품에 지급하면 안 됨")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    s = FakeSession(exec_results=[[], []])  # 결제 없음, 상품 없음
    await payment.grant_pack(s, UID_UUID, "com.unknown", "tx-1", store="app_store")
    assert s.added == []


async def test_grant_pack_missing_ids_skips():
    s = FakeSession()
    await payment.grant_pack(s, UID_UUID, "", "", store="app_store")
    assert s.added == []
