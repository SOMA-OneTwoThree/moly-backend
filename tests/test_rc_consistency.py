"""SOMA-372 RC 웹훅 정합성 — 내구 inbox 엔진·음수 부채 회수·잔액 클램프·UNIQUE 판별.

핸들러 매핑 자체는 test_subscription.py, 여기서는 큰문제 차단 기전(inbox 전이·allow_negative·
소비 게이트·클램프·오류 구분)을 단위로 검증한다.
"""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import AppError
from app.core.pg import unique_violation
from app.services import economy, hay_ledger, subscription
from app.services.subscription import (
    DEPENDENCY_MISSING,
    HANDLED,
    NO_OP,
    PERMANENT_FAILURE,
    TRANSFER_MANUAL,
    HandlerResult,
)

UID = uuid.uuid4()


# ─────────────────────────────────────────────────────────────
# 1. 음수 부채 회수 + 소비 게이트(큰문제3 완전 회수)
# ─────────────────────────────────────────────────────────────
class _LedgerSession:
    def __init__(self, profile):
        self.profile = profile
        self.added = []

    async def get(self, model, key, **kw):
        return self.profile

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


async def test_apply_allow_negative_goes_into_debt():
    """회수 경로(allow_negative=True)는 잔액을 음수(부채)로 내려 증정 소비분까지 완전 회수."""
    prof = SimpleNamespace(hay_balance=300)
    tx = await hay_ledger.apply(_LedgerSession(prof), UID, "refund_revoke", -1000, allow_negative=True)
    assert prof.hay_balance == -700 and tx.balance_after == -700


async def test_apply_consumption_gate_blocks_overdraw():
    """소비 경로는 기본 allow_negative=False — 잔액 초과 차감은 402(게이트 유지)."""
    prof = SimpleNamespace(hay_balance=300)
    with pytest.raises(AppError) as e:
        await hay_ledger.apply(_LedgerSession(prof), UID, "shop_purchase", -1000)
    assert e.value.code == "INSUFFICIENT_HAY"


async def test_apply_recovery_offsets_debt():
    """복구(admin_adjustment +)가 부채를 산술 정확히 상계한다."""
    prof = SimpleNamespace(hay_balance=-700)
    tx = await hay_ledger.apply(_LedgerSession(prof), UID, "admin_adjustment", 1000, allow_negative=True)
    assert prof.hay_balance == 300 and tx.balance_after == 300


# ─────────────────────────────────────────────────────────────
# 2. 잔액 클램프(음수 부채는 클라에 0으로)
# ─────────────────────────────────────────────────────────────
async def test_get_wallet_clamps_debt(monkeypatch):
    async def _lp(session, uid):
        return SimpleNamespace(hay_balance=-700)

    monkeypatch.setattr(economy, "_load_profile", _lp)
    assert (await economy.get_wallet(None, str(UID)))["balance"] == 0


async def test_list_transactions_clamps_balance_after(monkeypatch):
    async def _lp(session, uid):
        return SimpleNamespace(id=UID)

    rows = [SimpleNamespace(id=2, type="refund_revoke", amount=-1000, balance_after=-700,
                            created_at=None)]

    class _S:
        async def execute(self, stmt):
            class _R:
                def scalars(self_inner):
                    return SimpleNamespace(all=lambda: rows)
            return _R()

    monkeypatch.setattr(economy, "_load_profile", _lp)
    out = await economy.list_transactions(_S(), str(UID))
    assert out["data"][0]["balance_after"] == 0  # 음수 부채 0 하한
    assert out["data"][0]["amount"] == -1000     # 원장 amount는 원본 유지


# ─────────────────────────────────────────────────────────────
# 3. UNIQUE 위반 판별(오류 구분 — 광범위 except 은폐 금지)
# ─────────────────────────────────────────────────────────────
class _Orig(Exception):
    def __init__(self, sqlstate, constraint_name=None):
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def test_unique_violation_matches_constraint():
    exc = IntegrityError("s", {}, _Orig("23505", "subscriptions_original_transaction_id_key"))
    assert unique_violation(exc, "subscriptions_original_transaction_id_key")
    assert not unique_violation(exc, "other_constraint")


def test_unique_violation_non_unique_state_false():
    exc = IntegrityError("s", {}, _Orig("23502"))  # not_null_violation
    assert not unique_violation(exc, "subscriptions_original_transaction_id_key")


def test_unique_violation_any_constraint():
    exc = IntegrityError("s", {}, _Orig("23505", "c"))
    assert unique_violation(exc)  # 제약명 미지정 → 23505이면 True


# --- 실 asyncpg 구조 회귀(핵심): wrapper엔 sqlstate만, constraint_name은 __cause__에만 ---
class _AsyncpgCause(Exception):
    """asyncpg 원 예외(UniqueViolationError) 흉내 — constraint_name·sqlstate 보유."""
    def __init__(self, constraint_name):
        self.constraint_name = constraint_name
        self.sqlstate = "23505"


class _AsyncpgWrapper(Exception):
    """SQLAlchemy asyncpg 어댑터가 exc.orig로 노출하는 wrapper 흉내 — sqlstate만 있고
    constraint_name은 없다. 원 예외는 __cause__로만 연결된다(실측 구조)."""
    def __init__(self, sqlstate, cause):
        self.sqlstate = sqlstate
        self.__cause__ = cause


def test_unique_violation_asyncpg_constraint_on_cause():
    """실 asyncpg IntegrityError 구조 재현 회귀(SOMA-372 CRITICAL): constraint_name이 wrapper가
    아닌 __cause__에만 있어도 판별해야 한다. 예전엔 wrapper만 봐서 항상 False였고, 이를 못 잡은
    이유는 기존 테스트(_Orig)가 constraint_name을 wrapper에 직접 넣은 가짜 구조였기 때문이다."""
    cause = _AsyncpgCause("subscriptions_original_transaction_id_key")
    wrapper = _AsyncpgWrapper("23505", cause)  # wrapper엔 constraint_name 없음
    exc = IntegrityError("s", {}, wrapper)
    assert not hasattr(wrapper, "constraint_name")  # 구조 전제 확인
    assert unique_violation(exc, "subscriptions_original_transaction_id_key")
    assert not unique_violation(exc, "other_constraint")
    assert unique_violation(exc)  # 제약명 미지정이면 23505만으로 True


def test_unique_violation_asyncpg_non_unique_state_on_wrapper():
    """wrapper.sqlstate가 23505가 아니면(예: 23503 FK 위반) constraint_name이 있어도 False."""
    cause = _AsyncpgCause("some_fk")
    wrapper = _AsyncpgWrapper("23503", cause)  # foreign_key_violation
    exc = IntegrityError("s", {}, wrapper)
    assert not unique_violation(exc, "some_fk")
    assert not unique_violation(exc)


# ─────────────────────────────────────────────────────────────
# 4. inbox 엔진 — process_event 전이표
# ─────────────────────────────────────────────────────────────
class _InboxSession:
    """첫 execute = claim select(row 반환), 이후 execute(update) = 빈 결과. commit/rollback 카운트."""
    def __init__(self, row):
        self._row = row
        self._n = 0
        self.committed = 0
        self.rolled_back = 0

    async def execute(self, stmt):
        self._n += 1

        class _R:
            def __init__(self, items):
                self._items = items

            def scalars(self_inner):
                items = self_inner._items

                class _Sc:
                    def first(self_s):
                        return items[0] if items else None

                    def all(self_s):
                        return list(items)

                return _Sc()

        return _R([self._row] if (self._n == 1 and self._row is not None) else [])

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1


def _inbox_row():
    return SimpleNamespace(event_id="evt-1", payload={"type": "TEST", "id": "evt-1"},
                           status="pending", attempts=0, processed_at=None, last_error=None)


async def test_process_event_skipped_when_not_pending():
    """claim이 0행(이미 처리·다른 처리자 잠금) → skipped."""
    assert await subscription.process_event(_InboxSession(None), "evt-1") == "skipped"


async def test_process_event_handled_commits_processed(monkeypatch):
    row = _inbox_row()

    async def _h(session, event):
        return HandlerResult(HANDLED, None)

    monkeypatch.setattr(subscription, "handle_revenuecat_event", _h)
    s = _InboxSession(row)
    assert await subscription.process_event(s, "evt-1") == HANDLED
    assert row.status == "processed" and s.committed >= 1


async def test_process_event_no_op_records_reason(monkeypatch):
    row = _inbox_row()

    async def _h(session, event):
        return HandlerResult(NO_OP, "옛 상태 skip")

    monkeypatch.setattr(subscription, "handle_revenuecat_event", _h)
    s = _InboxSession(row)
    assert await subscription.process_event(s, "evt-1") == NO_OP
    assert row.status == "processed" and row.last_error == "옛 상태 skip"  # durable reason(은폐 아님)


async def test_process_event_sandbox_transfer_is_processed_without_economy_change():
    """SANDBOX TRANSFER 실핸 경로: 수동 failed가 아니라 durable processed/no-op으로 종료."""
    row = _inbox_row()
    row.payload = {
        "id": "evt-1",
        "type": "TRANSFER",
        "environment": "SANDBOX",
        "transferred_from": ["test-old"],
        "transferred_to": ["test-new"],
    }
    s = _InboxSession(row)

    assert await subscription.process_event(s, "evt-1") == NO_OP
    assert row.status == "processed"
    assert row.processed_at is not None
    assert "SANDBOX TRANSFER" in (row.last_error or "")
    assert s.rolled_back == 0 and s.committed == 1


async def test_process_event_dependency_missing_rolls_back_keeps_pending(monkeypatch):
    async def _h(session, event):
        return HandlerResult(DEPENDENCY_MISSING, "선행 결제 없음")

    monkeypatch.setattr(subscription, "handle_revenuecat_event", _h)
    s = _InboxSession(_inbox_row())
    assert await subscription.process_event(s, "evt-1") == DEPENDENCY_MISSING
    assert s.rolled_back >= 1 and s.committed >= 1  # 부분 경제변경 폐기 후 별도 tx 기록


async def test_process_event_permanent_failure(monkeypatch):
    async def _h(session, event):
        return HandlerResult(PERMANENT_FAILURE, "미등록 상품")

    monkeypatch.setattr(subscription, "handle_revenuecat_event", _h)
    s = _InboxSession(_inbox_row())
    assert await subscription.process_event(s, "evt-1") == PERMANENT_FAILURE
    assert s.rolled_back >= 1


async def test_process_event_transfer(monkeypatch):
    async def _h(session, event):
        return HandlerResult(TRANSFER_MANUAL, "TRANSFER 수동")

    monkeypatch.setattr(subscription, "handle_revenuecat_event", _h)
    assert await subscription.process_event(_InboxSession(_inbox_row()), "evt-1") == TRANSFER_MANUAL


async def test_process_event_unexpected_exception_retries(monkeypatch):
    """예상 밖 예외(DB 오류 등) → 롤백 후 별도 tx attempts+1(재시도) — 유실 없음."""
    async def _h(session, event):
        raise RuntimeError("boom")

    monkeypatch.setattr(subscription, "handle_revenuecat_event", _h)
    s = _InboxSession(_inbox_row())
    assert await subscription.process_event(s, "evt-1") == "exception"
    assert s.rolled_back >= 1 and s.committed >= 1  # attempts 갱신은 별도 tx


async def test_ingest_event_inserts_and_processes(monkeypatch):
    """엔드포인트 진입 — inbox insert 커밋 후 process_event 소비(중복은 ON CONFLICT로 멱등)."""
    seen = {}

    async def _pe(session, event_id):
        seen["event_id"] = event_id
        return "handled"

    monkeypatch.setattr(subscription, "process_event", _pe)
    s = _InboxSession(None)  # insert·commit 후 process_event는 monkeypatched
    res = await subscription.ingest_event(s, "evt-9", {"type": "TEST", "id": "evt-9"})
    assert res == "handled" and seen["event_id"] == "evt-9" and s.committed >= 1
