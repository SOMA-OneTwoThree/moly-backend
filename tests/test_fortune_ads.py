"""운세 광고는 서명 필드 전체를 대조하고 건초 경로와 분리한다."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import uuid
import inspect

from fastapi.testclient import TestClient

from app.config import settings
from app.core.db import get_session
from app.main import app
from app.models.fortune import DailyFortune, FortuneAdSession, FortuneProfile
from app.services import ads, ads_ssv, fortune_ads


class NoDatabaseSession:
    async def get(self, *args, **kwargs):  # pragma: no cover - 호출되면 테스트 실패
        raise AssertionError("invalid signed fields must fail before database access")


async def test_fortune_ssv_rejects_bad_custom_data_before_database():
    result = await fortune_ads.verify_from_ssv(
        NoDatabaseSession(),
        custom_data="not-fortune",
        transaction_id="tx-1",
        signed_user_id=str(uuid.uuid4()),
        ad_unit="unit",
        reward_item="fortune_unlock",
        reward_amount="1",
    )
    assert result == "invalid_session"


async def test_fortune_ssv_fails_closed_when_placement_allowlist_is_empty(monkeypatch):
    monkeypatch.setattr(settings, "fortune_ad_unit_ids", "")
    result = await fortune_ads.verify_from_ssv(
        NoDatabaseSession(),
        custom_data=f"fortune:{uuid.uuid4()}",
        transaction_id="tx-1",
        signed_user_id=str(uuid.uuid4()),
        ad_unit="unit",
        reward_item=settings.fortune_ad_reward_item,
        reward_amount=str(settings.fortune_ad_reward_amount),
    )
    assert result == "invalid_placement"


async def test_fortune_ssv_checks_reward_contract_before_database(monkeypatch):
    monkeypatch.setattr(settings, "fortune_ad_unit_ids", "unit-a")
    result = await fortune_ads.verify_from_ssv(
        NoDatabaseSession(),
        custom_data=f"fortune:{uuid.uuid4()}",
        transaction_id="tx-1",
        signed_user_id=str(uuid.uuid4()),
        ad_unit="unit-a",
        reward_item="hay",
        reward_amount="20",
    )
    assert result == "invalid_reward"


async def _dummy_session():
    yield None


def test_signed_fortune_prefix_dispatches_without_touching_hay(monkeypatch):
    sid = uuid.uuid4()
    uid = uuid.uuid4()
    captured = {}

    async def verify(_raw_query):
        return ads_ssv.VerifiedSsvPayload(
            key_id="1",
            parameters={
                "custom_data": f"fortune:{sid}",
                "transaction_id": "tx-1",
                "user_id": str(uid),
                "ad_unit": "unit-a",
                "reward_item": "fortune_unlock",
                "reward_amount": "1",
            },
        )

    async def fortune_verify(_session, **kwargs):
        captured.update(kwargs)
        return "verified"

    async def hay_grant(*_args, **_kwargs):
        raise AssertionError("fortune callback must not enter hay grant path")

    monkeypatch.setattr(ads_ssv, "verify_and_parse", verify)
    monkeypatch.setattr(fortune_ads, "verify_from_ssv", fortune_verify)
    monkeypatch.setattr(ads, "grant_from_ssv", hay_grant)
    app.dependency_overrides[get_session] = _dummy_session
    try:
        response = TestClient(app).get(
            "/webhooks/ad-ssv",
            params={
                "custom_data": f"fortune:{sid}",
                "transaction_id": "tx-1",
                "user_id": str(uid),
                "ad_unit": "unit-a",
                "reward_item": "fortune_unlock",
                "reward_amount": "1",
                "signature": "sig",
                "key_id": "1",
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "result": "verified"}
    assert captured["signed_user_id"] == str(uid)
    assert captured["ad_unit"] == "unit-a"


async def test_verified_ssv_immediately_unlocks_current_daily_result(monkeypatch):
    uid = uuid.uuid4()
    sid = uuid.uuid4()
    now = datetime(2026, 8, 27, 1, tzinfo=timezone.utc)
    ad = SimpleNamespace(
        session_id=sid,
        user_id=uid,
        fortune_date=date(2026, 8, 27),
        verified=False,
        ssv_transaction_id=None,
        verified_at=None,
        expires_at=now + timedelta(minutes=10),
    )
    profile = SimpleNamespace(revision=4)
    daily = SimpleNamespace(
        fortune_date=date(2026, 8, 27),
        timezone_snapshot="Asia/Seoul",
        profile_revision=4,
        result_schema_version=3,
        semantic_result={"schema_version": 3},
        copy_by_locale={"ko": {}},
        unlock_state="locked",
        unlock_source=None,
        unlocked_at=None,
        revealed_at=None,
        updated_at=None,
    )

    class Session:
        committed = False

        async def get(self, model, _key, **_kwargs):
            if model is FortuneAdSession:
                return ad
            if model is FortuneProfile:
                return profile
            if model is DailyFortune:
                return daily
            raise AssertionError(model)

        async def commit(self):
            self.committed = True

        async def rollback(self):  # pragma: no cover - unexpected failure path
            raise AssertionError("rollback was not expected")

    async def noop(*_args, **_kwargs):
        return None

    async def account(*_args, **_kwargs):
        return SimpleNamespace(timezone="Asia/Seoul")

    session = Session()
    monkeypatch.setattr(settings, "fortune_ad_unit_ids", "unit-a")
    monkeypatch.setattr(settings, "fortune_ad_reward_item", "fortune_unlock")
    monkeypatch.setattr(settings, "fortune_ad_reward_amount", 1)
    monkeypatch.setattr(fortune_ads, "advisory_xact_lock", noop)
    monkeypatch.setattr(fortune_ads.privacy, "ensure_subject_active", noop)
    monkeypatch.setattr(fortune_ads, "_load_profile", account)
    result = await fortune_ads.verify_from_ssv(
        session,
        custom_data=f"fortune:{sid}",
        transaction_id="tx-success",
        signed_user_id=str(uid),
        ad_unit="unit-a",
        reward_item="fortune_unlock",
        reward_amount="1",
        now_utc=now,
    )
    assert result == "verified"
    assert session.committed
    assert ad.verified and ad.ssv_transaction_id == "tx-success"
    assert daily.unlock_state == "unlocked"
    assert daily.unlock_source == "rewarded_ad"
    assert daily.unlocked_at == now and daily.revealed_at == now

    # 광고 재생 중 프로필이 바뀌면 공개 권한만 보존하고 다음 reveal이 새 snapshot을 만든다.
    sid = uuid.uuid4()
    ad = SimpleNamespace(
        session_id=sid,
        user_id=uid,
        fortune_date=date(2026, 8, 27),
        verified=False,
        ssv_transaction_id=None,
        verified_at=None,
        expires_at=now + timedelta(minutes=10),
    )
    profile.revision = 5
    daily.unlock_state = "locked"
    daily.unlock_source = daily.unlocked_at = daily.revealed_at = None
    stale_result = await fortune_ads.verify_from_ssv(
        session,
        custom_data=f"fortune:{sid}",
        transaction_id="tx-after-profile-change",
        signed_user_id=str(uid),
        ad_unit="unit-a",
        reward_item="fortune_unlock",
        reward_amount="1",
        now_utc=now,
    )
    assert stale_result == "verified"
    assert daily.unlock_state == "unlocked" and daily.revealed_at is None


def test_expired_session_cleanup_is_bounded_lock_safe_and_wired_to_worker():
    readiness_sql = "".join(str(fortune_ads._FORTUNE_AD_SESSIONS_REGCLASS).split()).upper()
    assert "TO_REGCLASS('PUBLIC.FORTUNE_AD_SESSIONS')" in readiness_sql
    sql = "".join(str(fortune_ads._DELETE_EXPIRED_SESSIONS).split()).upper()
    assert "ORDERBYEXPIRES_AT,SESSION_ID" in sql
    assert "FORUPDATESKIPLOCKED" in sql and "LIMIT500" in sql
    assert "INTERVAL'7DAYS'" in sql
    from worker import consumer

    source = inspect.getsource(consumer.reaper_loop)
    assert "fortune_ads.cleanup_expired_sessions" in source
    assert "if settings.fortune_enabled" not in source


async def test_cleanup_skips_before_migration_and_runs_while_feature_is_off(monkeypatch):
    class Result:
        def __init__(self, *, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = rows

        def scalar_one_or_none(self):
            return self.scalar

        def all(self):
            return list(self.rows)

    class Session:
        def __init__(self, table):
            self.table = table
            self.statements = []
            self.commits = 0

        async def execute(self, statement):
            self.statements.append(statement)
            if statement is fortune_ads._FORTUNE_AD_SESSIONS_REGCLASS:
                return Result(scalar=self.table)
            assert statement is fortune_ads._DELETE_EXPIRED_SESSIONS
            return Result(rows=((uuid.uuid4(),), (uuid.uuid4(),)))

        async def commit(self):
            self.commits += 1

    monkeypatch.setattr(settings, "fortune_enabled", False)

    before_migration = Session(None)
    assert await fortune_ads.cleanup_expired_sessions(before_migration) == 0
    assert before_migration.statements == [fortune_ads._FORTUNE_AD_SESSIONS_REGCLASS]
    assert before_migration.commits == 1

    after_migration = Session("fortune_ad_sessions")
    assert await fortune_ads.cleanup_expired_sessions(after_migration) == 2
    assert after_migration.statements == [
        fortune_ads._FORTUNE_AD_SESSIONS_REGCLASS,
        fortune_ads._DELETE_EXPIRED_SESSIONS,
    ]
    assert after_migration.commits == 1


async def test_ssv_duplicate_owner_and_expiry_fail_closed(monkeypatch):
    uid = uuid.uuid4()
    sid = uuid.uuid4()
    now = datetime(2026, 8, 27, 1, tzinfo=timezone.utc)

    class Session:
        def __init__(self, row):
            self.row = row

        async def get(self, model, _key, **_kwargs):
            assert model is FortuneAdSession
            return self.row

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "fortune_ad_unit_ids", "unit-a")
    monkeypatch.setattr(settings, "fortune_ad_reward_item", "fortune_unlock")
    monkeypatch.setattr(settings, "fortune_ad_reward_amount", 1)
    monkeypatch.setattr(fortune_ads, "advisory_xact_lock", noop)

    def row(**changes):
        value = dict(
            user_id=uid,
            verified=False,
            ssv_transaction_id=None,
            expires_at=now + timedelta(minutes=10),
        )
        value.update(changes)
        return SimpleNamespace(**value)

    async def verify(session, *, signed_user_id=str(uid), transaction_id="tx-1"):
        return await fortune_ads.verify_from_ssv(
            session,
            custom_data=f"fortune:{sid}",
            transaction_id=transaction_id,
            signed_user_id=signed_user_id,
            ad_unit="unit-a",
            reward_item="fortune_unlock",
            reward_amount="1",
            now_utc=now,
        )

    assert await verify(Session(row()), signed_user_id=str(uuid.uuid4())) == "owner_mismatch"
    assert await verify(Session(row(verified=True, ssv_transaction_id="tx-1"))) == "duplicate"
    assert (
        await verify(
            Session(row(verified=True, ssv_transaction_id="tx-original")),
            transaction_id="tx-replay",
        )
        == "session_used"
    )
    assert await verify(Session(row(expires_at=now - timedelta(seconds=1)))) == "expired"
