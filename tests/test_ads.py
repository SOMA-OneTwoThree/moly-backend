"""광고 — AdMob SSV 서명검증(실 ECDSA) + 세션 발급/자동 지급 흐름 + 인증."""
import base64
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.main import app
from app.services import ads, ads_ssv, economy, hay_ledger

UID = "11111111-1111-1111-1111-111111111111"
UID_UUID = uuid.UUID(UID)
SID = str(uuid.uuid4())
CONTENT = "ad_network=x&transaction_id=t1&custom_data=" + SID + "&reward_amount=1&timestamp=123"


# --- SSV 서명검증 ---
@pytest.fixture
def signed():
    priv = ec.generate_private_key(ec.SECP256R1())
    sig = priv.sign(CONTENT.encode(), ec.ECDSA(hashes.SHA256()))
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    raw_query = f"{CONTENT}&signature={sig_b64}&key_id=1234"
    return SimpleNamespace(sig_b64=sig_b64, pem=pem, raw_query=raw_query)


def _keys(d):
    async def f(**kw):
        return d
    return f


async def test_ssv_verify_valid(monkeypatch, signed):
    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({"1234": signed.pem}))
    assert await ads_ssv.verify(signed.raw_query, "1234", signed.sig_b64) is True


async def test_ssv_verify_tampered(monkeypatch, signed):
    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({"1234": signed.pem}))
    tampered = signed.raw_query.replace("reward_amount=1", "reward_amount=999")
    assert await ads_ssv.verify(tampered, "1234", signed.sig_b64) is False


async def test_ssv_verify_unknown_key(monkeypatch, signed):
    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({}))
    assert await ads_ssv.verify(signed.raw_query, "1234", signed.sig_b64) is False


# --- 세션 발급 / 자동 지급 ---
class _AdResult:
    """execute 결과 mock — scalar()만 유의미(보상 커서 func.max)."""

    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class FakeSession:
    def __init__(self, get_obj=None, cursor=None):
        self.get_obj = get_obj
        self.cursor = cursor  # _reward_cursor(func.max) 반환값. None=보상 이력 없음
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def get(self, model, key, **kw):
        return self.get_obj

    async def execute(self, stmt, params=None):
        # advisory lock(SELECT)·_reward_cursor(func.max) 등 — 커서만 유의미하게 돌려준다.
        return _AdResult(self.cursor)

    def add(self, obj):
        self.added.append(obj)

    async def rollback(self):
        self.rolled_back = True

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        if getattr(obj, "session_id", None) is None:
            obj.session_id = uuid.uuid4()


def _sess_row(**over):
    base = dict(session_id=uuid.UUID(SID), user_id=UID_UUID, activity_date=date(2026, 7, 5),
                granted=False, ssv_transaction_id=None)
    base.update(over)
    return SimpleNamespace(**base)


def _patch(monkeypatch, ad_count=3, balance=660):
    async def _daily(session, uid, ad):
        return SimpleNamespace(ad_reward_count=ad_count)

    async def _apply(session, uid, t, amt, **kw):
        return balance

    monkeypatch.setattr(economy, "_daily", _daily)
    monkeypatch.setattr(hay_ledger, "apply", _apply)


async def test_create_session_success(monkeypatch):
    _patch(monkeypatch, ad_count=3)

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, timezone="Asia/Seoul")

    monkeypatch.setattr(ads, "_load_profile", _lp)
    out = await ads.create_session(FakeSession(), UID)
    assert out["admob_user_id"] == UID
    assert out["views_used"] == 3 and out["views_limit"] == economy.AD_DAILY_LIMIT
    assert out["reward_session_id"]  # 발급됨


async def test_create_session_limit_429(monkeypatch):
    _patch(monkeypatch, ad_count=economy.AD_DAILY_LIMIT)

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, timezone="Asia/Seoul")

    monkeypatch.setattr(ads, "_load_profile", _lp)
    from app.core.errors import AppError
    with pytest.raises(AppError) as e:
        await ads.create_session(FakeSession(), UID)
    assert e.value.code == "AD_LIMIT_REACHED"


async def test_grant_success(monkeypatch):
    _patch(monkeypatch, ad_count=3)
    row = _sess_row()
    s = FakeSession(get_obj=row)
    assert await ads.grant_from_ssv(s, SID, "t1") == "granted"
    assert row.granted is True and row.ssv_transaction_id == "t1" and s.committed


async def test_grant_already_granted_skip(monkeypatch):
    async def _apply(*a, **k):
        raise AssertionError("이미 지급된 세션 재지급 금지")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    row = _sess_row(granted=True)
    s = FakeSession(get_obj=row)
    assert await ads.grant_from_ssv(s, SID, "t2") == "duplicate"  # 재전송 — 무시
    assert s.committed is False


async def test_grant_limit_no_pay(monkeypatch):
    async def _apply(*a, **k):
        raise AssertionError("한도 초과 시 지급 금지")

    async def _daily(session, uid, ad):
        return SimpleNamespace(ad_reward_count=economy.AD_DAILY_LIMIT)

    monkeypatch.setattr(economy, "_daily", _daily)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    row = _sess_row()
    assert await ads.grant_from_ssv(FakeSession(get_obj=row), SID, "t1") == "daily_limit"
    assert row.granted is False  # 미지급


async def test_grant_session_not_found_skip():
    s = FakeSession(get_obj=None)
    assert await ads.grant_from_ssv(s, SID, "t1") == "session_not_found"  # 무시, 에러 없음
    assert s.committed is False


async def test_grant_bad_session_id_skip():
    s = FakeSession()
    assert await ads.grant_from_ssv(s, "not-a-uuid", "t1") == "invalid_session"  # 형식 오류
    assert s.committed is False


class _FakeOrig(Exception):
    """asyncpg UniqueViolation 흉내 — pg.unique_violation이 판별하는 sqlstate·constraint_name."""
    def __init__(self, sqlstate="23505", constraint_name=None):
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def _unique_exc(constraint):
    from sqlalchemy.exc import IntegrityError
    return IntegrityError("stmt", {}, _FakeOrig(constraint_name=constraint))


async def test_grant_transaction_conflict_rollback(monkeypatch):
    """같은 transaction_id가 다른 세션으로 이미 지급 — UNIQUE 충돌 롤백, 멱등."""
    _patch(monkeypatch, ad_count=3)

    class ConflictSession(FakeSession):
        rolled_back = False

        async def commit(self):
            raise _unique_exc("reward_ad_sessions_ssv_transaction_id_key")

        async def rollback(self):
            self.rolled_back = True

    s = ConflictSession(get_obj=_sess_row())
    assert await ads.grant_from_ssv(s, SID, "t1") == "transaction_conflict"
    assert s.rolled_back is True


async def test_grant_transaction_conflict_at_flush(monkeypatch):
    """UNIQUE 충돌은 원장 apply 내부 flush에서도 터진다 — commit 전이라도 500이 아니라 멱등."""
    async def _daily(session, uid, ad):
        return SimpleNamespace(ad_reward_count=3)

    async def _apply(*a, **k):
        raise _unique_exc("reward_ad_sessions_ssv_transaction_id_key")

    monkeypatch.setattr(economy, "_daily", _daily)
    monkeypatch.setattr(hay_ledger, "apply", _apply)

    class RollbackSession(FakeSession):
        rolled_back = False

        async def rollback(self):
            self.rolled_back = True

    s = RollbackSession(get_obj=_sess_row())
    assert await ads.grant_from_ssv(s, SID, "t1") == "transaction_conflict"
    assert s.rolled_back is True and s.committed is False


async def test_grant_unexpected_integrity_error_reraises(monkeypatch):
    """예상 밖 IntegrityError(다른 제약·NULL/FK)는 transaction_conflict로 위장하지 않고 전파(은폐 금지)."""
    from sqlalchemy.exc import IntegrityError

    _patch(monkeypatch, ad_count=3)

    class ConflictSession(FakeSession):
        async def commit(self):
            raise IntegrityError("stmt", {}, _FakeOrig(sqlstate="23502"))  # not_null_violation

        async def rollback(self):
            pass

    with pytest.raises(IntegrityError):
        await ads.grant_from_ssv(ConflictSession(get_obj=_sess_row()), SID, "t1")


# --- 엔드포인트 ---
async def _dummy_session():
    yield None


def test_ssv_webhook_missing_params():
    r = TestClient(app).get("/webhooks/ad-ssv?key_id=1")
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"


@pytest.mark.parametrize("outcome", ["granted", "session_not_found"])
def test_ssv_webhook_result_in_body(monkeypatch, outcome):
    """서명 통과 후 처리 결과는 HTTP 200 유지 + body result로 구분."""
    async def _verify(*a, **k):
        return True

    async def _grant(session, sid, tx):
        return outcome

    monkeypatch.setattr(ads_ssv, "verify", _verify)
    monkeypatch.setattr(ads, "grant_from_ssv", _grant)
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).get(
            f"/webhooks/ad-ssv?custom_data={SID}&transaction_id=t1&signature=s&key_id=1"
        )
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and r.json() == {"status": "ok", "result": outcome}


def test_reward_ad_session_requires_auth():
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).post("/reward-ad-sessions")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


async def test_ssv_key_force_refetch_is_throttled(monkeypatch):
    """미등록 key_id 강제 재조회는 최소 간격 스로틀 — 서명 없는 refetch 폭주(DoS) 방지(SOMA-376)."""
    calls = {"n": 0}

    class _Resp:
        def json(self):
            return {"keys": []}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls["n"] += 1
            return _Resp()

    monkeypatch.setattr(ads_ssv.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ads_ssv, "_keys_cache", None)
    monkeypatch.setattr(ads_ssv, "_keys_fetched_at", 0.0)
    monkeypatch.setattr(ads_ssv, "_last_force_at", 0.0)
    # 첫 조회(캐시 없음)만 실제 fetch, 이후 강제 재조회는 스로틀로 재fetch 안 함.
    await ads_ssv._get_keys(force=True)
    await ads_ssv._get_keys(force=True)
    await ads_ssv._get_keys(force=True)
    assert calls["n"] == 1


# --- SOMA-375: tz 역행 재수령 차단 ---
async def test_create_session_rejects_tz_regression(monkeypatch):
    # 미래 커서 → 현재 reward_date는 과거 → 세션 발급 거부(409 ALREADY_CLAIMED)
    _patch(monkeypatch, ad_count=0)

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, timezone="Asia/Seoul")

    monkeypatch.setattr(ads, "_load_profile", _lp)
    session = FakeSession(cursor=date(2999, 1, 1))
    with pytest.raises(AppError) as e:
        await ads.create_session(session, UID)
    assert e.value.code == "ALREADY_CLAIMED"
    assert session.added == [] and session.committed is False  # 세션 미발급


async def test_grant_ssv_stale_reward_window_no_pay(monkeypatch):
    # 선발급된 과거 세션(현재 커서보다 뒤)의 SSV → 200 유지(예외 아님), 미지급
    _patch(monkeypatch, ad_count=0)
    row = _sess_row(activity_date=date(2026, 7, 5))
    session = FakeSession(get_obj=row, cursor=date(2999, 1, 1))
    out = await ads.grant_from_ssv(session, SID, "t-stale")
    assert out == "stale_reward_window"
    assert session.committed is False and row.granted is False  # 미지급·미커밋
