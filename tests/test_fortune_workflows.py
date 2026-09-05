"""운세 프로필·snapshot·공개 권한 상태 전이 회귀 테스트."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.core.db import get_session
from app.core.security import get_current_user
from app.main import app
from app.models.fortune import DailyFortune, FortuneAdSession, FortuneProfile
from app.schemas.fortune import FortuneProfilePut
from app.services import fortune, fortune_ads

UID = uuid.UUID("10000000-0000-4000-8000-000000000099")
TODAY = date(2026, 8, 27)
NOW = datetime(2026, 8, 27, 1, tzinfo=timezone.utc)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _MemorySession:
    def __init__(self, *, profile=None, daily=None):
        self.profile = profile
        self.daily = daily
        self.ads: list[FortuneAdSession] = []
        self.commits = 0

    async def get(self, model, key, **_kwargs):
        if model is FortuneProfile:
            return (
                self.profile if self.profile is not None and self.profile.user_id == key else None
            )
        if model is DailyFortune:
            return self.daily if self.daily is not None and self.daily.user_id == key else None
        if model is FortuneAdSession:
            return next((row for row in self.ads if row.session_id == key), None)
        raise AssertionError(model)

    def add(self, row):
        if isinstance(row, FortuneProfile):
            self.profile = row
        elif isinstance(row, DailyFortune):
            self.daily = row
        elif isinstance(row, FortuneAdSession):
            self.ads.append(row)
        else:  # pragma: no cover - service contract guard
            raise AssertionError(type(row))

    async def execute(self, _statement):
        if "fortune_ad_sessions.verified IS true" in str(_statement):
            params = _statement.compile().params
            return _ScalarResult(next((
                row.session_id for row in self.ads
                if row.verified and row.fortune_date == params["fortune_date_1"]
            ), None))
        # 광고 세션 조회는 생성된 단일 행만 찾는다.
        return _ScalarResult(self.ads[0] if self.ads else None)

    async def commit(self):
        self.commits += 1

    async def refresh(self, row):
        if isinstance(row, FortuneAdSession) and row.session_id is None:
            row.session_id = uuid.UUID("20000000-0000-4000-8000-000000000001")

    async def delete(self, row):
        if row is self.profile:
            self.profile = None
            self.daily = None


@pytest.fixture
def enabled(monkeypatch):
    async def noop(*_args, **_kwargs):
        return None

    async def account(*_args, **_kwargs):
        return SimpleNamespace(timezone="Asia/Seoul", language="ko")

    monkeypatch.setattr(fortune, "_ready", lambda: True)
    monkeypatch.setattr(fortune.privacy, "ensure_subject_active", noop)
    monkeypatch.setattr(fortune, "advisory_xact_lock", noop)
    monkeypatch.setattr(fortune, "_load_profile", account)


async def test_profile_same_put_is_noop_and_change_invalidates_but_preserves_unlock(enabled):
    session = _MemorySession()
    request = FortuneProfilePut(birth_date=date(2002, 12, 13), gender="man")
    created = await fortune.put_profile(session, str(UID), request, now_utc=NOW)
    assert created == {
        "profile": {"gender": "man", "birth_date": date(2002, 12, 13), "revision": 1},
        "result_invalidated": False,
        "unlock_preserved": False,
    }

    unlocked_at = datetime(2026, 8, 27, 1, 5, tzinfo=timezone.utc)
    session.daily = DailyFortune(
        user_id=UID,
        fortune_date=TODAY,
        timezone_snapshot="Asia/Seoul",
        profile_revision=1,
        result_schema_version=3,
        semantic_result={"schema_version": 3},
        copy_by_locale={"ko": {}},
        unlock_state="unlocked",
        unlock_source="rewarded_ad",
        unlocked_at=unlocked_at,
        revealed_at=unlocked_at,
    )
    unchanged = await fortune.put_profile(session, str(UID), request, now_utc=NOW)
    assert unchanged["profile"]["revision"] == 1
    assert not unchanged["result_invalidated"] and not unchanged["unlock_preserved"]

    changed = await fortune.put_profile(
        session,
        str(UID),
        FortuneProfilePut(birth_date=date(2002, 12, 13), gender="woman"),
        now_utc=NOW,
    )
    assert changed["profile"]["revision"] == 2
    assert changed["result_invalidated"] and changed["unlock_preserved"]
    assert session.daily.unlock_source == "rewarded_ad"
    assert session.daily.unlocked_at == unlocked_at


async def test_stale_unlocked_snapshot_status_is_unseen_with_access_preserved(enabled):
    profile = FortuneProfile(user_id=UID, gender="woman", birth_date=date(2002, 12, 13), revision=2)
    daily = DailyFortune(
        user_id=UID,
        fortune_date=TODAY,
        timezone_snapshot="Asia/Seoul",
        profile_revision=1,
        result_schema_version=2,
        semantic_result={"schema_version": 2},
        copy_by_locale={"ko": {"old": True}},
        unlock_state="unlocked",
        unlock_source="rewarded_ad",
        unlocked_at=NOW,
        revealed_at=NOW,
    )
    value = await fortune.status(
        _MemorySession(profile=profile, daily=daily), str(UID), locale="ko", now_utc=NOW
    )
    assert value == {
        "available": True,
        "state": "unseen",
        "access": "unlocked_today",
        "local_date": TODAY,
    }


async def test_reveal_rebuilds_stale_snapshot_without_losing_unlock_or_changing_again(enabled):
    original_unlock = datetime(2026, 8, 27, 0, 30, tzinfo=timezone.utc)
    profile = FortuneProfile(user_id=UID, gender="woman", birth_date=date(2002, 12, 13), revision=2)
    daily = DailyFortune(
        user_id=UID,
        fortune_date=TODAY,
        timezone_snapshot="Asia/Seoul",
        profile_revision=1,
        result_schema_version=2,
        semantic_result={"schema_version": 2},
        copy_by_locale={"ko": {"old": True}},
        unlock_state="unlocked",
        unlock_source="rewarded_ad",
        unlocked_at=original_unlock,
        revealed_at=original_unlock,
    )
    session = _MemorySession(profile=profile, daily=daily)
    first = await fortune.reveal(session, str(UID), locale="ko", now_utc=NOW)
    second = await fortune.reveal(session, str(UID), locale="ja", now_utc=NOW)
    assert first["result"]["overall"]["score"] == second["result"]["overall"]["score"]
    assert first["state"] == "revealed" and first["access"] == "unlocked_today"
    assert first["result"]["schema_version"] == 3 and first["result"]["locale"] == "ko"
    assert second["result"]["locale"] == "ja"
    assert session.daily.profile_revision == 2
    assert session.daily.unlock_source == "rewarded_ad"
    assert session.daily.unlocked_at == original_unlock


@pytest.mark.parametrize(
    ("access", "plan", "expected_state"),
    [
        ("ad_required", "free", "locked"),
        ("included", "monthly", "revealed"),
        ("included", "trial", "revealed"),
    ],
)
async def test_first_reveal_exposes_basic_copy_and_included_plan_exposes_detail(
    enabled, monkeypatch, access, plan, expected_state
):
    async def fixed_access(*_args, **_kwargs):
        return access, plan

    monkeypatch.setattr(fortune, "_access", fixed_access)
    profile = FortuneProfile(user_id=UID, gender="man", birth_date=date(2002, 12, 13), revision=1)
    session = _MemorySession(profile=profile)
    value = await fortune.reveal(session, str(UID), locale="ko", now_utc=NOW)
    assert value["state"] == expected_state
    assert value["result"]["overall"]["score"] == 47
    if expected_state == "locked":
        assert set(value["result"]["overall"]) == {"score", "headline", "do", "pause"}
        assert set(value["result"]) == {"schema_version", "locale", "overall", "lucky_color"}
        assert session.daily.revealed_at is None
    else:
        assert value["access"] == "included"
        assert len(value["result"]["overall"]["flow"]) == 3
        assert len(value["result"]["categories"]) == 4
        assert session.daily.unlock_source == ("trial" if plan == "trial" else "subscription")


@pytest.mark.parametrize("locale", ["ko", "en", "ja"])
async def test_basic_result_is_public_but_detail_requires_verified_ad(enabled, monkeypatch, locale):
    profile = FortuneProfile(user_id=UID, gender="man", birth_date=date(2002, 12, 13), revision=1)
    session = _MemorySession(profile=profile)

    async def free_plan(*_args, **_kwargs):
        return "free"

    async def database():
        yield session

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(fortune, "datetime", Clock)
    monkeypatch.setattr(fortune.gating, "resolve_plan", free_plan)
    monkeypatch.setattr(fortune_ads, "advisory_xact_lock", fortune.advisory_xact_lock)
    monkeypatch.setattr(fortune_ads, "_load_profile", fortune._load_profile)
    monkeypatch.setattr(settings, "fortune_ad_unit_ids", "unit-a")
    app.dependency_overrides[get_current_user] = lambda: str(UID)
    app.dependency_overrides[get_session] = database
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"X-App-Locale": locale},
        ) as client:
            unseen = (await client.get("/daily-fortune/status")).json()
            assert unseen["state"] == "unseen" and "result" not in unseen
            response = await client.post("/daily-fortune/reveal")
            assert response.status_code == 200
            basic = response.json()
            assert basic["state"] == "locked" and basic["access"] == "ad_required"
            assert basic["result"]["locale"] == locale
            assert set(basic["result"]) == {"schema_version", "locale", "overall", "lucky_color"}
            assert set(basic["result"]["overall"]) == {"score", "headline", "do", "pause"}
            assert (await client.get("/daily-fortune/status")).json() == {
                "available": True, **basic,
            }
            ad_response = await client.post("/daily-fortune/ad-sessions", json={
                "client_request_id": "30000000-0000-4000-8000-000000000001",
            })
            assert ad_response.status_code == 201
            # 발급·미완료 광고만으로는 상세 정보가 해금되지 않는다.
            assert (await client.post("/daily-fortune/reveal")).json() == basic
            assert (await client.get("/daily-fortune/status")).json()["result"] == basic["result"]
            stored_copy = session.daily.copy_by_locale[locale]
            assert len(stored_copy["overall"]["flow"]) == 3
            assert len(stored_copy["categories"]) == 4
            ad = ad_response.json()
            assert await fortune_ads.verify_from_ssv(
                session, custom_data=ad["custom_data"], transaction_id="verified-tx",
                signed_user_id=str(UID), ad_unit="unit-a",
                reward_item=settings.fortune_ad_reward_item,
                reward_amount=str(settings.fortune_ad_reward_amount), now_utc=NOW,
            ) == "verified"
            unlocked = (await client.get("/daily-fortune/status")).json()
            assert unlocked["state"] == "revealed" and unlocked["access"] == "unlocked_today"
            full = unlocked["result"]
            assert full["overall"]["flow"] == stored_copy["overall"]["flow"]
            assert len(full["categories"]) == 4
            assert {key: full["overall"][key] for key in basic["result"]["overall"]} == (
                basic["result"]["overall"]
            )
            assert full["lucky_color"] == basic["result"]["lucky_color"]
            assert (await client.post("/daily-fortune/reveal")).json()["result"] == full
    finally:
        app.dependency_overrides.clear()

    # 다음 날에는 기본 정보가 다시 공개되고 상세 권한은 초기화된다.
    tomorrow = await fortune.reveal(
        session, str(UID), locale=locale, now_utc=NOW + timedelta(days=1),
    )
    assert tomorrow["state"] == "locked" and tomorrow["access"] == "ad_required"
    assert "flow" not in tomorrow["result"]["overall"]
    assert "categories" not in tomorrow["result"]


async def test_ad_session_client_request_id_is_idempotent(enabled, monkeypatch):
    async def ad_required(*_args, **_kwargs):
        return "ad_required", "free"

    monkeypatch.setattr(fortune, "_access", ad_required)
    profile = FortuneProfile(user_id=UID, gender="man", birth_date=date(2002, 12, 13), revision=1)
    semantic, copies = fortune._build_result(
        profile=profile,
        today=TODAY,
        timezone_name="Asia/Seoul",
    )
    daily = DailyFortune(
        user_id=UID,
        fortune_date=TODAY,
        timezone_snapshot="Asia/Seoul",
        profile_revision=1,
        result_schema_version=3,
        semantic_result=semantic,
        copy_by_locale=copies,
        unlock_state="locked",
        unlock_source=None,
        unlocked_at=None,
        revealed_at=None,
    )
    session = _MemorySession(profile=profile, daily=daily)
    client_request_id = uuid.UUID("30000000-0000-4000-8000-000000000001")
    first, created = await fortune.create_ad_session(
        session, str(UID), client_request_id=client_request_id, now_utc=NOW
    )
    second, created_again = await fortune.create_ad_session(
        session, str(UID), client_request_id=client_request_id, now_utc=NOW
    )
    assert created and not created_again
    assert first == second
    assert first["custom_data"] == f"fortune:{first['session_id']}"
