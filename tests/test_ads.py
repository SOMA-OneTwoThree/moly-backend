"""광고 — AdMob SSV 서명검증(실 ECDSA) + 세션 발급/자동 지급 흐름 + 인증."""
import base64
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.config import settings
from app.core.db import get_session
from app.core.errors import AppError
from app.main import app
from app.services import ads, ads_ssv, economy, hay_ledger

UID = "11111111-1111-1111-1111-111111111111"
UID_UUID = uuid.UUID(UID)
SID = str(uuid.uuid4())
CONTENT = "ad_network=x&transaction_id=t1&custom_data=" + SID + "&reward_amount=1&timestamp=123"
VALID_SSV_FIELDS = {
    "signed_user_id": UID,
    "ad_unit": "hay-unit",
    "reward_item": "Reward",
    "reward_amount": "1",
}


@pytest.fixture(autouse=True)
def _hay_reward_contract(monkeypatch):
    monkeypatch.setattr(settings, "hay_ad_unit_ids", "hay-unit")
    monkeypatch.setattr(settings, "hay_ad_reward_item", "Reward")
    monkeypatch.setattr(settings, "hay_ad_reward_amount", 1)


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
    payload = await ads_ssv.verify_and_parse(signed.raw_query)
    assert payload is not None
    assert payload.key_id == "1234"
    assert payload.get("custom_data") == SID
    assert payload.get("transaction_id") == "t1"


async def test_ssv_verify_tampered(monkeypatch, signed):
    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({"1234": signed.pem}))
    tampered = signed.raw_query.replace("reward_amount=1", "reward_amount=999")
    assert await ads_ssv.verify_and_parse(tampered) is None


async def test_ssv_verify_unknown_key(monkeypatch, signed):
    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({}))
    assert await ads_ssv.verify_and_parse(signed.raw_query) is None


async def test_ssv_verify_rejects_critical_duplicate(monkeypatch):
    """서명 자체가 유효해도 critical field가 중복된 모호한 payload는 거절한다."""
    priv = ec.generate_private_key(ec.SECP256R1())
    content = f"custom_data={SID}&custom_data={uuid.uuid4()}&transaction_id=t1"
    sig = priv.sign(content.encode(), ec.ECDSA(hashes.SHA256()))
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({"1234": pem}))

    assert await ads_ssv.verify_and_parse(
        f"{content}&signature={sig_b64}&key_id=1234"
    ) is None


@pytest.mark.parametrize(
    "raw_query",
    [
        "x=%ZZ&signature=" + ("a" * 80) + "&key_id=1234",
        "x=1&signature=short&key_id=1234",
        "x=1&signature=" + ("a" * 80) + "&key_id=not-numeric",
        "x=" + ("a" * ads_ssv._MAX_QUERY_LENGTH) + "&signature=" + ("a" * 80) + "&key_id=1",
    ],
)
async def test_ssv_verify_rejects_malformed_or_oversized_envelope(raw_query):
    assert await ads_ssv.verify_and_parse(raw_query) is None


@pytest.mark.parametrize("duplicate_auth", ["signature=shadow", "key_id=9999"])
async def test_ssv_verify_rejects_duplicate_auth_fields(duplicate_auth):
    """서명된 prefix 안에도 signature/key_id가 있으면 모호한 envelope라 거절한다."""
    priv = ec.generate_private_key(ec.SECP256R1())
    content = f"custom_data={SID}&transaction_id=t1&{duplicate_auth}"
    sig = priv.sign(content.encode(), ec.ECDSA(hashes.SHA256()))
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")

    assert await ads_ssv.verify_and_parse(
        f"{content}&signature={sig_b64}&key_id=1234"
    ) is None


# --- 세션 발급 / 자동 지급 ---
class _AdResult:
    """execute 결과 mock — scalar()만 유의미(보상 커서 func.max)."""

    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def mappings(self):
        return self

    def first(self):
        return None


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
                granted=False, ssv_transaction_id=None,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))
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


async def test_create_session_reuses_active_pending_session(monkeypatch):
    """같은 사용자·활동일의 유효 pending 세션은 새 행 없이 재사용한다."""
    _patch(monkeypatch, ad_count=2)
    pending_id = uuid.uuid4()

    async def _lp(session, user_id):
        return SimpleNamespace(id=UID_UUID, timezone="Asia/Seoul")

    class PendingResult(_AdResult):
        def first(self):
            return {"session_id": pending_id}

    class PendingSession(FakeSession):
        async def execute(self, stmt, params=None):
            if stmt is ads._REUSE_PENDING_SESSION:
                assert params is not None
                assert params["user_id"] == UID_UUID
                return PendingResult(None)
            return await super().execute(stmt, params)

    monkeypatch.setattr(ads, "_load_profile", _lp)
    session = PendingSession()
    out = await ads.create_session(session, UID)

    assert out["reward_session_id"] == str(pending_id)
    assert out["views_used"] == 2
    assert session.added == []
    assert session.committed is True


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
    assert await ads.grant_from_ssv(s, SID, "t1", **VALID_SSV_FIELDS) == "granted"
    assert row.granted is True and row.ssv_transaction_id == "t1" and s.committed


@pytest.mark.parametrize(
    ("setting", "value", "expected"),
    [
        ("hay_ad_unit_ids", "", "invalid_placement"),
        ("hay_ad_unit_ids", "another-unit", "invalid_placement"),
        ("hay_ad_reward_item", "hay_reward", "invalid_reward"),
        ("hay_ad_reward_amount", 20, "invalid_reward"),
    ],
)
async def test_grant_rejects_wrong_hay_contract_before_database(
    monkeypatch, setting, value, expected
):
    monkeypatch.setattr(settings, setting, value)

    class NoDatabaseSession:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("invalid signed contract must fail before database access")

    assert await ads.grant_from_ssv(
        NoDatabaseSession(), SID, "t1", **VALID_SSV_FIELDS
    ) == expected


async def test_grant_rejects_signed_user_that_does_not_own_session(monkeypatch):
    async def _lock(*_args, **_kwargs):
        raise AssertionError("owner mismatch must fail before acquiring the user lock")

    monkeypatch.setattr(ads, "advisory_xact_lock", _lock)
    fields = {**VALID_SSV_FIELDS, "signed_user_id": str(uuid.uuid4())}
    assert await ads.grant_from_ssv(
        FakeSession(get_obj=_sess_row()), SID, "t1", **fields
    ) == "owner_mismatch"


async def test_grant_already_granted_skip(monkeypatch):
    async def _apply(*a, **k):
        raise AssertionError("이미 지급된 세션 재지급 금지")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    row = _sess_row(granted=True)
    s = FakeSession(get_obj=row)
    assert await ads.grant_from_ssv(
        s, SID, "t2", **VALID_SSV_FIELDS
    ) == "duplicate"  # 재전송 — 무시
    assert s.committed is False


async def test_grant_limit_no_pay(monkeypatch):
    async def _apply(*a, **k):
        raise AssertionError("한도 초과 시 지급 금지")

    async def _daily(session, uid, ad):
        return SimpleNamespace(ad_reward_count=economy.AD_DAILY_LIMIT)

    monkeypatch.setattr(economy, "_daily", _daily)
    monkeypatch.setattr(hay_ledger, "apply", _apply)
    row = _sess_row()
    assert await ads.grant_from_ssv(
        FakeSession(get_obj=row), SID, "t1", **VALID_SSV_FIELDS
    ) == "daily_limit"
    assert row.granted is False  # 미지급


async def test_grant_expired_session_no_pay(monkeypatch):
    """만료된 pending 세션은 원장·일일 카운터를 건드리지 않고 영구 거절한다."""
    async def _apply(*_args, **_kwargs):
        raise AssertionError("expired session must not grant a reward")

    async def _daily(*_args, **_kwargs):
        raise AssertionError("expiry must be checked before daily state")

    monkeypatch.setattr(hay_ledger, "apply", _apply)
    monkeypatch.setattr(economy, "_daily", _daily)
    row = _sess_row(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    session = FakeSession(get_obj=row)

    assert await ads.grant_from_ssv(
        session, SID, "t-expired", **VALID_SSV_FIELDS
    ) == "expired"
    assert row.granted is False
    assert session.committed is False


async def test_grant_session_not_found_skip():
    s = FakeSession(get_obj=None)
    assert await ads.grant_from_ssv(
        s, SID, "t1", **VALID_SSV_FIELDS
    ) == "session_not_found"  # 무시, 에러 없음
    assert s.committed is False


async def test_grant_bad_session_id_skip():
    s = FakeSession()
    assert await ads.grant_from_ssv(
        s, "not-a-uuid", "t1", **VALID_SSV_FIELDS
    ) == "invalid_session"  # 형식 오류
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
    assert await ads.grant_from_ssv(
        s, SID, "t1", **VALID_SSV_FIELDS
    ) == "transaction_conflict"
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
    assert await ads.grant_from_ssv(
        s, SID, "t1", **VALID_SSV_FIELDS
    ) == "transaction_conflict"
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
        await ads.grant_from_ssv(
            ConflictSession(get_obj=_sess_row()), SID, "t1", **VALID_SSV_FIELDS
        )


# --- 엔드포인트 ---
async def _dummy_session():
    yield None


def test_ssv_webhook_missing_params():
    r = TestClient(app).get("/webhooks/ad-ssv?key_id=1")
    assert r.status_code == 422 and r.json()["error"]["code"] == "AD_VERIFY_FAILED"


def test_ssv_webhook_signed_probe_with_custom_data_without_transaction_returns_200(monkeypatch):
    """custom_data가 있지만 transaction이 없는 실제 서명 probe는 no-op 200이다."""
    priv = ec.generate_private_key(ec.SECP256R1())
    content = (
        "ad_network=5450213213286189855&ad_unit=probe-unit"
        f"&custom_data={SID}&reward_amount=1&reward_item=reward&timestamp=123"
    )
    sig = priv.sign(content.encode(), ec.ECDSA(hashes.SHA256()))
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

    async def _grant(*_args, **_kwargs):
        raise AssertionError("verification-only callback must not grant a reward")

    async def _fortune_grant(*_args, **_kwargs):
        raise AssertionError("verification-only callback must not unlock fortune")

    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({"1234": pem}))
    monkeypatch.setattr(ads, "grant_from_ssv", _grant)
    monkeypatch.setattr("app.api.ads.fortune_ads.verify_from_ssv", _fortune_grant)
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).get(
            f"/webhooks/ad-ssv?{content}&signature={sig_b64}&key_id=1234"
        )
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "result": "invalid_session"}


def test_ssv_webhook_unsigned_probe_is_rejected(monkeypatch):
    """유효한 서명 뒤에 붙인 unsigned business fields는 보상에 쓰이지 않는다."""
    priv = ec.generate_private_key(ec.SECP256R1())
    content = "ad_network=5450213213286189855&ad_unit=probe-unit&timestamp=123"
    sig = priv.sign(content.encode(), ec.ECDSA(hashes.SHA256()))
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

    async def _grant(*_args, **_kwargs):
        raise AssertionError("unsigned suffix must not grant a reward")

    monkeypatch.setattr(ads_ssv, "_get_keys", _keys({"1234": pem}))
    monkeypatch.setattr(ads, "grant_from_ssv", _grant)
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).get(
            f"/webhooks/ad-ssv?{content}&signature={sig_b64}&key_id=1234"
            f"&custom_data={SID}&transaction_id=unsigned-tx"
        )
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "AD_VERIFY_FAILED"


@pytest.mark.parametrize("outcome", ["granted", "session_not_found"])
def test_ssv_webhook_result_in_body(monkeypatch, outcome):
    """서명 통과 후 처리 결과는 HTTP 200 유지 + body result로 구분."""
    async def _verify(_raw_query):
        return ads_ssv.VerifiedSsvPayload(
            key_id="1",
            parameters={"custom_data": SID, "transaction_id": "t1"},
        )

    async def _grant(session, sid, tx, **_signed_fields):
        return outcome

    monkeypatch.setattr(ads_ssv, "verify_and_parse", _verify)
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


def test_reward_ad_session_cleanup_is_bounded_and_lock_safe():
    """reaper끼리 경합하지 않고 오래된 세션을 상태와 무관하게 bounded 정리한다."""
    sql = "".join(str(ads._DELETE_EXPIRED_SESSIONS).split()).upper()
    assert "WHEREEXPIRES_AT<NOW()-INTERVAL'7DAYS'" in sql
    assert "GRANTED=FALSE" not in sql
    assert "ORDERBYEXPIRES_AT,SESSION_ID" in sql
    assert "FORUPDATESKIPLOCKED" in sql
    assert "LIMIT500" in sql


async def test_cleanup_expired_reward_ad_sessions_commits_deleted_count():
    class Result:
        def all(self):
            return [(uuid.uuid4(),), (uuid.uuid4(),)]

    class Session:
        statements = []
        commits = 0

        async def execute(self, statement):
            self.statements.append(statement)
            return Result()

        async def commit(self):
            self.commits += 1

    session = Session()
    assert await ads.cleanup_expired_sessions(session) == 2
    assert session.statements == [ads._DELETE_EXPIRED_SESSIONS]
    assert session.commits == 1


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
    out = await ads.grant_from_ssv(session, SID, "t-stale", **VALID_SSV_FIELDS)
    assert out == "stale_reward_window"
    assert session.committed is False and row.granted is False  # 미지급·미커밋
