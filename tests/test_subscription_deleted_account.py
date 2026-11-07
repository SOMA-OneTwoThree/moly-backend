"""탈퇴가 끝난 계정의 스토어 결제: 증거가 모두 있을 때만 결제 기록·첫 구독 건초를 건너뛴다.

메모리 대역만 쓴다(공급자·DB 호출 없음).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.services import privacy, subscription, subscription_processing as processing

ANON = "$RCAnonymousID:0123456789abcdef"
NOW = datetime.now(timezone.utc)


class Session:
    def __init__(self, profile=None):
        self.profile = profile

    async def get(self, model, identifier, **kwargs):
        return self.profile


def _case(owner, *, kind="RENEWAL"):
    tx = "otx" if kind == "INITIAL_PURCHASE" else "renew-tx"
    event = {"id": "deleted-purchase", "type": kind, "app_user_id": str(owner),
             "original_app_user_id": ANON, "aliases": [str(owner), ANON],
             "transaction_id": tx, "original_transaction_id": "otx", "period_type": "NORMAL",
             "is_family_share": False, "price_in_purchased_currency": 59000, "currency": "KRW",
             "environment": "PRODUCTION", "store": "APP_STORE"}
    target = processing.Target(None, {"id": "rc-object", "customer_id": str(owner),
                                      "original_customer_id": ANON, "store_subscription_identifier": tx},
                               [{"id": "otx", "purchased_at": 1}, {"id": tx, "purchased_at": 2}], {})
    prepared = processing.Prepared([], None, NOW, targets=[target], bonus_review="eligible")
    return event, prepared


def _env(monkeypatch, status=privacy.STATUS_DELETED, *, other=False, transfer=False,
         chain=("otx", "renew-tx"), conflict=False):
    monkeypatch.setattr(privacy, "load_barrier",
                        AsyncMock(return_value=privacy.BarrierState(status, 0, None)))
    monkeypatch.setattr(processing, "_other_user_in_chain", AsyncMock(return_value=other))
    monkeypatch.setattr(processing, "_transfer_involving", AsyncMock(return_value=transfer))
    monkeypatch.setattr(processing, "_chain_transaction_ids",
                        AsyncMock(return_value=None if chain is None else list(chain)))
    monkeypatch.setattr(processing, "_chain_conflict", AsyncMock(return_value=conflict))
    record, grant = AsyncMock(), AsyncMock()
    monkeypatch.setattr(subscription, "_record_subscription_payment", record)
    monkeypatch.setattr(subscription, "_grant_if_first", grant)
    return record, grant


@pytest.mark.parametrize("kind", ["RENEWAL", "INITIAL_PURCHASE"])
async def test_deleted_account_purchase_is_skipped_and_closes_as_processed(monkeypatch, kind):
    owner = uuid.uuid4()
    event, prepared = _case(owner, kind=kind)
    record, grant = _env(monkeypatch)
    state = processing.new_state(event)
    state["access_state"] = "verified"
    await processing._finance(Session(), event, state, None, None, prepared, received_at=NOW)
    record.assert_not_awaited()
    grant.assert_not_awaited()
    assert state["payment_record"] == state["bonus"] == "skipped"
    assert state["reason"] == "deleted account purchase: no payment record or bonus"
    assert processing.finish_attempt(state) == ("processed", False)
    # 전체 TRANSFER 이력과 같은 원거래의 다른 이름·다른 사용자 결제/구독까지 본다.
    assert processing._transfer_involving.await_args.args[1:] == (str(owner), processing._EPOCH)
    assert processing._other_user_in_chain.await_args.args[1:] == ("otx", owner)
    assert processing._chain_conflict.await_args.args[1] == owner


@pytest.mark.parametrize("case", [
    "barrier_missing", "barrier_active", "profile_exists", "family_share", "family_unknown",
    "customer_differs", "customer_anonymous", "uuid_alias", "uuid_original_app_user",
    "prepared_before", "target_before", "tx_not_in_target", "two_targets", "no_transaction",
    "other_name_in_chain", "transfer", "chain_overflow", "chain_conflict", "free_trial", "cancellation",
])
async def test_deleted_rule_keeps_existing_retry_when_any_proof_is_missing(monkeypatch, case):
    owner = uuid.uuid4()
    event, prepared = _case(owner)
    target = prepared.targets[0]
    status, other, transfer, chain, conflict, profile = privacy.STATUS_DELETED, False, False, ("otx",), False, None
    if case == "barrier_missing":
        status = privacy.STATUS_MISSING
    elif case == "barrier_active":
        status = privacy.STATUS_ACTIVE
    elif case == "profile_exists":
        profile = SimpleNamespace(id=owner)
    elif case == "family_share":
        event["is_family_share"] = True
    elif case == "family_unknown":
        event.pop("is_family_share")
    elif case == "customer_differs":
        target.snapshot["customer_id"] = str(uuid.uuid4())
    elif case == "customer_anonymous":
        target.snapshot["customer_id"] = ANON
    elif case == "uuid_alias":
        event["aliases"].append(str(uuid.uuid4()))
    elif case == "uuid_original_app_user":
        event["original_app_user_id"] = str(uuid.uuid4())
    elif case == "prepared_before":
        prepared.before = [{"id": "row", "user_id": str(uuid.uuid4())}]
    elif case == "target_before":
        prepared.targets[0] = processing.Target({"id": "row"}, target.snapshot, target.transactions, {})
    elif case == "tx_not_in_target":
        target.transactions[:] = [{"id": "otx"}]
    elif case == "two_targets":
        prepared.targets.append(processing.Target(None, dict(target.snapshot), list(target.transactions), {}))
    elif case == "no_transaction":
        event["transaction_id"] = ""
    elif case == "other_name_in_chain":
        other = True
    elif case == "transfer":
        transfer = True
    elif case == "chain_overflow":
        chain = None
    elif case == "chain_conflict":
        conflict = True
    elif case == "free_trial":
        event["period_type"] = "TRIAL"
    elif case == "cancellation":
        event["type"] = "CANCELLATION"
    record, grant = _env(monkeypatch, status, other=other, transfer=transfer, chain=chain, conflict=conflict)
    monkeypatch.setattr(subscription, "_mark_store_trial_offer_redeemed", AsyncMock())
    state = processing.new_state(event)
    await processing._finance(Session(profile), event, state, None, None, prepared, received_at=NOW)
    record.assert_not_awaited()
    grant.assert_not_awaited()
    assert "skipped" not in (state["payment_record"], state["bonus"])
    assert processing.HOLD_KEY not in state


async def test_expired_provider_read_or_existing_payment_is_not_judged(monkeypatch):
    owner = uuid.uuid4()
    event, prepared = _case(owner)
    _env(monkeypatch)
    judge = AsyncMock(return_value=processing._DELETED)
    monkeypatch.setattr(processing, "_deleted_owner_purchase", judge)
    prepared.targets = []  # 조회가 60초를 넘겨 비워진 경우
    state = processing.new_state(event)
    await processing._finance(Session(), event, state, None, None, prepared, received_at=NOW)
    judge.assert_not_awaited()
    assert state["payment_record"] == state["bonus"] == "pending"


async def test_deletion_in_progress_waits_without_spending_attempts(monkeypatch):
    owner = uuid.uuid4()
    event, prepared = _case(owner)
    record, grant = _env(monkeypatch, privacy.STATUS_DELETING)
    state = processing.new_state(event)
    state["access_state"] = "verified"
    for _ in range(8):
        await processing._finance(Session(), event, state, None, None, prepared, received_at=NOW)
        assert processing.finish_attempt(state) == ("pending", True)
        assert processing.HOLD_KEY not in state  # 저장되는 상태에는 남지 않는다
    assert state["tries"] == {}
    assert state["reason"] == "account deletion in progress"
    record.assert_not_awaited()
    grant.assert_not_awaited()
    # 탈퇴가 끝나면 다음 시도에서 건너뜀으로 끝난다.
    _env(monkeypatch)
    await processing._finance(Session(), event, state, None, None, prepared, received_at=NOW)
    assert processing.finish_attempt(state) == ("processed", False)


async def test_deletion_stuck_over_a_day_spends_attempts_and_ends_manual(monkeypatch):
    owner = uuid.uuid4()
    event, prepared = _case(owner)
    _env(monkeypatch, privacy.STATUS_DELETING)
    state = processing.new_state(event)
    state["access_state"] = "verified"
    old = NOW - timedelta(hours=25)
    results = []
    for _ in range(5):
        await processing._finance(Session(), event, state, None, None, prepared, received_at=old)
        results.append(processing.finish_attempt(state))
    assert results[-1] == ("failed", False)
    assert state["payment_record"] == state["bonus"] == "manual"


def test_hold_only_spares_finance_purchase_stages():
    state = processing.new_state({"type": "RENEWAL"})
    state[processing.HOLD_KEY] = True
    assert processing.finish_attempt(state) == ("pending", True)
    assert state["tries"] == {"access_state": 1}
    state[processing.HOLD_KEY] = "yes"  # True가 아니면 무시
    processing.finish_attempt(state)
    assert state["tries"] == {"access_state": 2, "payment_record": 1, "bonus": 1}


class _Capture:
    def __init__(self, rows=()):
        self.statements, self.rows = [], list(rows)

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: self.rows)


async def test_chain_check_reads_every_event_of_the_same_original_and_their_names():
    owner = uuid.uuid4()
    mine = (str(owner), ANON, [str(owner), ANON])
    session = _Capture([mine, (str(owner), None, None)])
    assert await processing._other_user_in_chain(session, "otx", owner) is False
    sql = str(session.statements[0].compile(dialect=postgresql.dialect(),
                                            compile_kwargs={"literal_binds": True}))
    assert "'otx'" in sql
    # 형식 검사를 위해 최초 사용자·별칭은 JSON 원형(payload['키'])으로 읽는다. 사용자 이름만 문자(->>)로 읽는다.
    assert "->> 'original_app_user_id'" not in sql and "payload['original_app_user_id']" in sql
    assert "->> 'aliases'" not in sql and "payload['aliases']" in sql
    assert "->> 'app_user_id'" in sql
    assert "status" not in sql  # 대기·실패 이벤트도 근거


@pytest.mark.parametrize("names", [
    ("OTHER", None, None),                                   # 익명·다른 이름으로 온 이벤트
    (None, None, None),                                      # 이름 없는 이벤트
    ("OWNER", str(uuid.uuid4()), None),                      # 최초 사용자가 다른 계정
    ("OWNER", ANON, ["OWNER", ANON, str(uuid.uuid4())]),     # 과거 이벤트 별칭에 다른 계정
    ("OWNER", None, "OWNER"),                                # 별칭 형식을 모름
    ("OWNER", None, ["OWNER", {"id": "x"}]),                 # 별칭에 문자열 아닌 값
    ("OWNER", None, ["OWNER", None]),
    ("OWNER", None, ["OWNER", 7]),
    ("OWNER", {"id": "x"}, None),                            # 최초 사용자 형식을 모름
])
async def test_any_other_account_in_past_events_blocks(names):
    owner = uuid.uuid4()
    def fill(value):
        if isinstance(value, list):
            return [fill(item) for item in value]
        return str(owner) if value == "OWNER" else ANON if value == "OTHER" else value
    rows = [(str(owner), ANON, [str(owner), ANON]), tuple(fill(v) for v in names)]
    assert await processing._other_user_in_chain(_Capture(rows), "otx", owner) is True


async def test_chain_check_overflow_blocks():
    owner = uuid.uuid4()
    rows = [(str(owner), None, None)] * (processing._CHAIN_ID_LIMIT + 1)
    assert await processing._other_user_in_chain(_Capture(rows), "otx", owner) is True


class _InboxSession:
    def __init__(self, row):
        self.row = row

    async def execute(self, statement):
        row = self.row
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row))

    async def commit(self):
        pass

    async def rollback(self):
        pass


@pytest.mark.parametrize("barrier", [privacy.STATUS_DELETING, privacy.STATUS_DELETED])
async def test_inbox_attempt_counter_is_not_spent_while_deletion_is_in_progress(monkeypatch, barrier):
    owner = uuid.uuid4()
    event, prepared = _case(owner)
    _env(monkeypatch, barrier)
    row = SimpleNamespace(event_id="evt", payload=event, status="pending", attempts=0, processing_state=None,
                          received_at=NOW, last_error=None, processed_at=None, next_attempt_at=None)

    async def apply(session, inbox_row, _prepared):
        state = processing.new_state(event)
        state["access_state"] = "verified"
        await processing._finance(session, event, state, None, None, prepared, received_at=inbox_row.received_at)
        inbox_row.processing_state = state

    monkeypatch.setattr(processing, "selected", AsyncMock(return_value=prepared))
    monkeypatch.setattr(processing, "fetch", AsyncMock(return_value=prepared))
    monkeypatch.setattr(processing, "apply", apply)
    session = _InboxSession(row)
    session.get = Session().get
    await subscription.process_event(session, "evt")
    assert processing.HOLD_KEY not in row.processing_state
    if barrier == privacy.STATUS_DELETING:
        assert (row.status, row.attempts, row.processing_state["tries"]) == ("pending", 0, {})
        assert row.next_attempt_at is not None
    else:
        assert (row.status, row.attempts, row.last_error) == ("processed", 0, None)
        assert row.processing_state["payment_record"] == row.processing_state["bonus"] == "skipped"
