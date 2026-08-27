"""운세 프로필·snapshot·공개 권한 상태 전이 회귀 테스트."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.models.fortune import DailyFortune, FortuneAdSession, FortuneProfile
from app.schemas.fortune import FortuneProfilePut
from app.services import fortune

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
        # 테스트 흐름에는 검증된 광고가 없고, 광고 세션 조회는 생성된 단일 행만 찾는다.
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
    [("ad_required", "free", "locked"), ("included", "monthly", "revealed")],
)
async def test_first_reveal_never_leaks_locked_copy_and_included_plan_reveals(
    enabled, monkeypatch, access, plan, expected_state
):
    async def fixed_access(*_args, **_kwargs):
        return access, plan

    monkeypatch.setattr(fortune, "_access", fixed_access)
    profile = FortuneProfile(user_id=UID, gender="man", birth_date=date(2002, 12, 13), revision=1)
    session = _MemorySession(profile=profile)
    value = await fortune.reveal(session, str(UID), locale="ko", now_utc=NOW)
    assert value["state"] == expected_state
    if expected_state == "locked":
        assert value == {"state": "locked", "access": "ad_required", "local_date": TODAY}
        assert session.daily.revealed_at is None
    else:
        assert value["access"] == "included"
        assert value["result"]["overall"]["score"] == 47
        assert session.daily.unlock_source == "subscription"


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
