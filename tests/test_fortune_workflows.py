"""운세 프로필·snapshot·공개 권한 상태 전이 회귀 테스트."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid
from urllib.parse import quote, unquote, urlencode

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.core.db import get_session
from app.core.security import get_current_user
from app.main import app
from app.models.fortune import DailyFortune, FortuneAdSession, FortuneProfile
from app.schemas.fortune import FortuneProfilePut
from app.services import ads_ssv, fortune, fortune_ads

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

    stored_account = SimpleNamespace(timezone="Asia/Seoul", language="ko", fortune_first_date=date.min)

    async def account(*_args, **_kwargs):
        return stored_account

    monkeypatch.setattr(fortune, "_ready", lambda: True)
    monkeypatch.setattr(fortune.settings, "environment", "development")
    monkeypatch.setattr(fortune.privacy, "ensure_subject_active", noop)
    monkeypatch.setattr(fortune, "advisory_xact_lock", noop)
    monkeypatch.setattr(fortune, "_load_profile", account)


@pytest.mark.parametrize("source", ["free", "launch", "signup_trial", "store_trial", "subscription"])
async def test_fortune_is_ad_free_for_paid_plans_and_signup_trials_only(enabled, monkeypatch, source):
    plan = {"free": "free", "launch": "trial", "signup_trial": "trial"}.get(source, "monthly")
    monkeypatch.setattr(fortune.gating, "resolve_entitlement", AsyncMock(
        return_value={"plan": plan, "entitlement_source": source}))
    p = FortuneProfile(user_id=UID, gender="man", birth_date=date(2002, 12, 13), revision=1)
    session = _MemorySession(profile=p)
    value = await fortune.reveal(session, str(UID), locale="en", now_utc=NOW)
    included = source in {"signup_trial", "store_trial", "subscription"}
    assert value["access"] == ("included" if included else "ad_required")
    assert value["state"] == ("revealed" if included else "locked")
    assert ("categories" in value["result"]) == included
    assert session.ads == []


async def test_profile_same_put_is_noop_and_birthday_change_invalidates_result_preserving_unlock(enabled):
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
        semantic_result={
            "schema_version": 4,
            "experience_version": fortune.fortune_scores.RULE_VERSION,
            "draw_algorithm_version": fortune.DRAW_ALGORITHM_VERSION,
        },
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
        FortuneProfilePut(birth_date=date(2000, 1, 1), gender="man"),
        now_utc=NOW,
    )
    assert changed["profile"]["revision"] == 2
    assert changed["result_invalidated"] and changed["unlock_preserved"]
    assert session.daily.unlock_source == "rewarded_ad"
    assert session.daily.unlocked_at == unlocked_at


@pytest.mark.parametrize("revision", [1, 2])
async def test_stale_unlocked_snapshot_status_is_unseen_with_access_preserved(enabled, revision):
    profile = FortuneProfile(user_id=UID, gender="woman", birth_date=date(2002, 12, 13), revision=revision)
    daily = DailyFortune(
        user_id=UID,
        fortune_date=TODAY,
        timezone_snapshot="Asia/Seoul",
        profile_revision=1,
        result_schema_version=3,
        semantic_result={"schema_version": 4},
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


@pytest.mark.parametrize("stale_reason", ["birthday_edit", "old_policy"])
async def test_reveal_rebuilds_stale_snapshot_without_losing_unlock_or_changing_again(enabled, stale_reason):
    original_unlock = datetime(2026, 8, 27, 0, 30, tzinfo=timezone.utc)
    profile = FortuneProfile(user_id=UID, gender="woman", birth_date=date(2002, 12, 13), revision=2)
    old_profile = FortuneProfile(
        user_id=UID, gender="woman",
        birth_date=date(2000, 1, 1) if stale_reason == "birthday_edit" else profile.birth_date,
        revision=1 if stale_reason == "birthday_edit" else profile.revision,
    )
    old_semantic, old_copy = fortune._build_result(
        profile=old_profile, today=TODAY, timezone_name="Asia/Seoul",
        experience_enabled=stale_reason != "old_policy",
    )
    expected_semantic, expected_copy = fortune._build_result(
        profile=profile, today=TODAY, timezone_name="Asia/Seoul",
    )
    daily = DailyFortune(
        user_id=UID,
        fortune_date=TODAY,
        timezone_snapshot="Asia/Seoul",
        profile_revision=old_profile.revision,
        result_schema_version=3,
        semantic_result=old_semantic,
        copy_by_locale=old_copy,
        copy_version="fortune-copy.v5-saved-snapshot.1",
        unlock_state="unlocked",
        unlock_source="rewarded_ad",
        unlocked_at=original_unlock,
        revealed_at=original_unlock,
    )
    session = _MemorySession(profile=profile, daily=daily)
    first = await fortune.reveal(session, str(UID), locale="ko", now_utc=NOW)
    assert session.daily.semantic_result == expected_semantic
    assert session.daily.copy_by_locale == expected_copy
    assert session.daily.copy_version == fortune.fortune_catalog.COPY_VERSION
    assert session.daily.semantic_result["copy_selection"] != old_semantic["copy_selection"]
    rebuilt_semantic = deepcopy(session.daily.semantic_result)
    rebuilt_copy = deepcopy(session.daily.copy_by_locale)
    second = await fortune.reveal(session, str(UID), locale="ja", now_utc=NOW)
    assert session.daily.semantic_result == rebuilt_semantic
    assert session.daily.copy_by_locale == rebuilt_copy
    assert first["result"]["overall"]["score"] == second["result"]["overall"]["score"]
    assert first["state"] == "revealed" and first["access"] == "unlocked_today"
    assert first["result"]["schema_version"] == 3 and first["result"]["locale"] == "ko"
    assert second["result"]["locale"] == "ja"
    assert session.daily.profile_revision == 2
    assert session.daily.unlock_source == "rewarded_ad"
    assert session.daily.unlocked_at == original_unlock
    account = await fortune._load_profile(session, str(UID))
    assert account.fortune_first_date == date.min


@pytest.mark.parametrize(
    ("plan", "expected_state"),
    [
        ("free", "locked"),
        ("trial", "locked"),
        ("monthly", "revealed"),
        ("yearly", "revealed"),
    ],
)
async def test_first_reveal_exposes_basic_copy_and_included_plan_exposes_detail(
    enabled, monkeypatch, plan, expected_state
):
    async def fixed_plan(*_args, **_kwargs):
        source = {"free": "free", "trial": "launch"}.get(plan, "subscription")
        return {"plan": plan, "entitlement_source": source}

    monkeypatch.setattr(fortune.gating, "resolve_entitlement", fixed_plan)
    profile = FortuneProfile(user_id=UID, gender="man", birth_date=date(2002, 12, 13), revision=1)
    session = _MemorySession(profile=profile)
    value = await fortune.reveal(session, str(UID), locale="ko", now_utc=NOW)
    assert value["state"] == expected_state
    # This returning account uses the new regular score policy. The API must
    # expose the persisted score consistently for every subscription tier.
    assert value["result"]["overall"]["score"] == 85
    assert value["result"]["overall"]["score"] == session.daily.semantic_result["overall"]["score"]
    assert value["result"]["overall"]["flow"] == session.daily.copy_by_locale["ko"]["overall"]["flow"]
    assert len(value["result"]["overall"]["flow"]) == 2
    assert session.daily.semantic_result["experience_mode"] == "regular"
    if expected_state == "locked":
        assert value["access"] == "ad_required"
        assert set(value["result"]["overall"]) == {"score", "headline", "flow", "do", "pause"}
        assert set(value["result"]) == {"schema_version", "locale", "overall", "lucky_color"}
        assert session.daily.revealed_at is None
    else:
        assert value["access"] == "included"
        assert len(value["result"]["categories"]) == 4
        assert session.daily.unlock_source == "subscription"


@pytest.mark.parametrize("locale", ["ko", "en", "ja"])
@pytest.mark.parametrize("plan", ["free", "trial"])
async def test_basic_result_is_public_but_detail_requires_verified_ad(
    enabled, monkeypatch, locale, plan
):
    profile = FortuneProfile(user_id=UID, gender="man", birth_date=date(2002, 12, 13), revision=1)
    session = _MemorySession(profile=profile)

    async def fixed_plan(*_args, **_kwargs):
        source = {"free": "free", "trial": "launch"}.get(plan, "subscription")
        return {"plan": plan, "entitlement_source": source}

    async def database():
        yield session

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(fortune, "datetime", Clock)
    monkeypatch.setattr(fortune_ads, "datetime", Clock)
    monkeypatch.setattr(fortune.gating, "resolve_entitlement", fixed_plan)
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
            assert set(basic["result"]["overall"]) == {"score", "headline", "flow", "do", "pause"}
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
            snapshot = deepcopy((session.daily.semantic_result, session.daily.copy_by_locale))
            assert basic["result"]["overall"]["flow"] == stored_copy["overall"]["flow"]
            assert len(stored_copy["overall"]["flow"]) == 2
            assert len(stored_copy["categories"]) == 4
            ad = ad_response.json()
            content = urlencode({
                "custom_data": ad["custom_data"], "transaction_id": "verified-tx",
                "user_id": str(UID), "ad_unit": "unit-a",
                "reward_item": settings.fortune_ad_reward_item,
                "reward_amount": str(settings.fortune_ad_reward_amount),
            }, quote_via=quote)
            assert "fortune%3A" in content
            private_key = ec.generate_private_key(ec.SECP256R1())
            signature = private_key.sign(unquote(content).encode(), ec.ECDSA(hashes.SHA256()))
            pem = private_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode()

            async def keys(**_kwargs):
                return {"1234": pem}

            monkeypatch.setattr(ads_ssv, "_get_keys", keys)
            sig_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")
            callback = await client.get(
                f"/webhooks/ad-ssv?{content}&signature={sig_b64}&key_id=1234"
            )
            assert callback.status_code == 200
            assert callback.json()["result"] == "verified"
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
            assert (session.daily.semantic_result, session.daily.copy_by_locale) == snapshot
    finally:
        app.dependency_overrides.clear()

    # 다음 날에는 기본 정보가 다시 공개되고 상세 권한은 초기화된다.
    tomorrow = await fortune.reveal(
        session, str(UID), locale=locale, now_utc=NOW + timedelta(days=1),
    )
    assert tomorrow["state"] == "locked" and tomorrow["access"] == "ad_required"
    assert tomorrow["result"]["overall"]["flow"] == (
        session.daily.copy_by_locale[locale]["overall"]["flow"]
    )
    assert "categories" not in tomorrow["result"]


@pytest.mark.parametrize("paragraph_count", [2, 3])
@pytest.mark.parametrize("unlocked", [False, True])
async def test_current_saved_snapshot_exposes_flow_without_rebuilding_or_changing_unlock(
    enabled, monkeypatch, paragraph_count, unlocked
):
    async def free_plan(*_args, **_kwargs):
        return {"plan": "free", "entitlement_source": "free"}

    monkeypatch.setattr(fortune.gating, "resolve_entitlement", free_plan)
    profile = FortuneProfile(user_id=UID, gender="man", birth_date=date(2002, 12, 13), revision=1)
    semantic, copies = fortune._build_result(
        profile=profile, today=TODAY, timezone_name="Asia/Seoul",
    )
    # Only the saved Korean locale is available in this older snapshot.
    saved_copy = deepcopy(copies["ko"])
    saved_copy["overall"]["flow"] = [f"저장된 총운 {index}." for index in range(paragraph_count)]
    saved_copy["lucky_color"] = {"key": "coral", "name": "코랄", "hex": "#FF7F6E"}
    original_unlock = NOW - timedelta(minutes=10) if unlocked else None
    daily = DailyFortune(
        user_id=UID,
        fortune_date=TODAY,
        timezone_snapshot="Asia/Seoul",
        profile_revision=1,
        result_schema_version=3,
        semantic_result=semantic,
        copy_by_locale={"ko": saved_copy},
        ephemeris_version="saved-ephemeris",
        rule_version="saved-rules",
        copy_version="fortune-copy.v6-editorial.1",
        unlock_state="unlocked" if unlocked else "locked",
        unlock_source="rewarded_ad" if unlocked else None,
        unlocked_at=original_unlock,
        revealed_at=original_unlock,
    )
    session = _MemorySession(profile=profile, daily=daily)
    snapshot = deepcopy((daily.semantic_result, daily.copy_by_locale))

    def must_not_rebuild(**_kwargs):  # pragma: no cover - snapshot must stay current
        raise AssertionError("current snapshot was rebuilt to expose overall.flow")

    monkeypatch.setattr(fortune, "_build_result", must_not_rebuild)
    for action in (fortune.status, fortune.reveal, fortune.status, fortune.reveal):
        value = await action(session, str(UID), locale="ja", now_utc=NOW)
        assert value["state"] == ("revealed" if unlocked else "locked")
        assert value["access"] == ("unlocked_today" if unlocked else "ad_required")
        assert value["result"]["locale"] == "ko"
        assert value["result"]["overall"] == {
            "score": semantic["overall"]["score"], **saved_copy["overall"],
        }
        assert value["result"]["lucky_color"] == saved_copy["lucky_color"]
        assert ("categories" in value["result"]) is unlocked
        assert value["versions"] == {
            "ephemeris": "saved-ephemeris", "rules": "saved-rules",
            "copy": "fortune-copy.v6-editorial.1",
        }
    assert (daily.semantic_result, daily.copy_by_locale) == snapshot
    assert daily.unlock_state == ("unlocked" if unlocked else "locked")
    assert daily.unlock_source == ("rewarded_ad" if unlocked else None)
    assert daily.unlocked_at == daily.revealed_at == original_unlock
    account = await fortune._load_profile(session, str(UID))
    assert account.fortune_first_date == date.min


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


# Approved saved v8 copy for the fixed birth date/day; verify either local writer version.
_V8_SAVED_COPY = json.loads(r'''{
  "ko": {
    "overall": {
      "headline": "곤란한 일을 벗어나 한시름 덜 수 있어.",
      "flow": [
        "혼자 풀지 못한 일을 다른 사람이 도와주거나 장소를 옮기면서 불편함이 줄어들 수 있어. 모두 해결되지는 않아도 전보다 편하게 지낼 수 있어.",
        "더 쉽게 할 방법을 알려주는 사람이 있다면 들어봐. 익숙한 곳이 불편하다면 다른 곳을 골라도 괜찮아."
      ],
      "do": "도움을 받아 다른 방법 시도하기.",
      "pause": "익숙하다고 불편한 곳에 계속 머무르기."
    },
    "categories": {
      "love": {
        "text": [
          "솔로라면 만날 약속 없이 연락만 해서 답답할 수 있어. 끝없이 기다릴 필요는 없어.",
          "연애 중이라면 혼자 양보하던 일로 불만이 터질 수 있어. 참기만 한다고 나아지지는 않아.",
          "이별 후라면 상대가 연락해도 재회 얘기는 피할 수 있어. 계속 답이 없다면 그만 기다려도 돼."
        ]
      },
      "money": {
        "text": [
          "급히 들은 소식 때문에 돈을 잘못 쓸 수 있어. 가격이 오른다거나 놓치면 손해라는 말에 마음이 급해지기 쉬운 날이야. 확인하지 않고 쓰면 뜻밖의 비용도 생길 수 있어. 전해 들은 말은 처음 이야기한 곳에 직접 물어봐."
        ]
      },
      "work": {
        "text": [
          "작은 과제나 미뤄둔 공부를 마치며 만족할 수 있어. 당장 시험이 없어도 배운 내용을 제대로 이해하고 넘어갈 수 있어.",
          "직장에서는 큰 칭찬이 없어도 맡은 일이 무리 없이 끝날 수 있어. 서로 도움을 주고받으며 편안하게 일할 수 있는 날이야."
        ]
      },
      "energy": {
        "text": [
          "몸 챙기는 일을 놓쳐 쉽게 지칠 수 있어. 입맛이나 기분에 따라 끼니를 넘기면 평소 일도 더 힘들 수 있는 날이야. 귀찮다고 식사를 계속 거르지는 마. 쉬어도 기운이 없고 불편함이 이어진다면 혼자 참지 말고 진료를 받아봐."
        ]
      }
    }
  },
  "en": {
    "overall": {
      "headline": "Getting out of a difficult situation could give you some relief.",
      "flow": [
        "You may be able to leave an uncomfortable situation today. Someone could help with a problem you couldn't solve alone, or moving somewhere else may reduce the trouble you're dealing with. Not every problem has to be over for things to feel easier than before.",
        "If someone can explain an easier way to move elsewhere, hear them out. It's fine to choose somewhere different if the familiar place has been uncomfortable."
      ],
      "do": "If someone explains an easier way to handle something you've struggled with alone, hear them out and accept their help.",
      "pause": "If there's somewhere you could be more at ease, don't keep staying in an uncomfortable place just because it's familiar."
    },
    "categories": {
      "love": {
        "text": [
          "If you're single, staying in touch without any plans to meet could become frustrating. If the other person keeps avoiding an answer to your feelings, the wait may grow longer. You don't have to wait indefinitely for the same answer.",
          "In a relationship, resentment could flare up again if only one of you has been compromising. If promises of change aren't followed by different behaviour, an argument may not end easily. Simply putting up with everything won't improve the relationship today.",
          "After a breakup, contact may continue without a decision to get back together. If your ex avoids that discussion and only comes to you when they need something, it's better to expect less. You can also choose to stop waiting for an answer and end contact."
        ]
      },
      "money": {
        "text": [
          "Money news heard in haste could lead to a wrong decision. Talk of rising prices or losing out if you miss a chance may make you rush today. Spending without checking the details could bring unexpected costs. Confirm the story with whoever made the original offer before judging it."
        ]
      },
      "work": {
        "text": [
          "Finishing a small assignment or some postponed studying could bring a satisfying result. Even without an imminent exam, it's a good day to understand the material properly before moving on.",
          "At work, your tasks could reach completion smoothly, even without much fanfare. Giving and receiving help within the team may make for a comfortable working day. Check what's been completed with your colleagues."
        ]
      },
      "energy": {
        "text": [
          "Neglecting to look after your body could leave you tiring easily. Letting your appetite or mood decide whether you eat properly may make the day harder to manage. Don't keep skipping meals because eating feels like a bother. If you still lack strength after rest or discomfort continues, have it checked instead of enduring it alone."
        ]
      }
    }
  },
  "ja": {
    "overall": {
      "headline": "困っていた状況を離れ、一息つけそうです。",
      "flow": [
        "居心地の悪い状況から、抜け出せるかもしれません。一人で解決できなかったことを助けてもらい、場所を変えて面倒が減ることもありそうです。すべて終わらなくても、以前より楽に過ごせるでしょう。",
        "楽に移れる方法を教えてくれる人がいたら、説明を聞きましょう。慣れた場所でも居心地が悪かったなら、ほかを選んで大丈夫です。"
      ],
      "do": "一人では難しかったことを楽に進める方法を教えてくれる人がいたら、説明を聞いて助けを借りてみましょう。",
      "pause": "もっと楽に過ごせる場所があるなら、慣れているというだけで、居心地の悪い場所にとどまらないようにしましょう。"
    },
    "categories": {
      "love": {
        "text": [
          "フリーの人は、会う約束もないまま連絡だけが続く関係に、もどかしさを感じそうです。相手が告白への返事を避け続けるなら、待つ時間がさらに延びるかもしれません。いつまでも同じ答えを待ち続ける必要はありません。",
          "交際中なら、一人ばかりが譲ってきたことへの不満が、また噴き出しそうです。変えると言っても行動が同じなら、けんかは簡単には収まりません。ただ我慢すれば関係がよくなる日ではなさそうです。",
          "別れた後なら、復縁を決めないまま、連絡だけが続くかもしれません。相手が復縁の話を避け、必要なときだけ頼ってくるなら、期待を小さくしたほうがよさそうです。答えのない待ち時間に区切りをつけ、連絡を終える選択もできます。"
        ]
      },
      "money": {
        "text": [
          "急に聞いたお金の話から、判断を誤るかもしれません。値上がりする、逃すと損をするといった言葉に、焦りやすい日です。実際の内容を調べずに使うと、思いがけない費用が付く可能性があります。人づての話は、その話を出した相手に確かめてから判断しましょう。"
        ]
      },
      "work": {
        "text": [
          "小さな課題や後回しにしていた勉強を終え、満足できる結果を得られそうです。すぐに試験がなくても、学んだ内容をしっかり理解してから次へ進むのによい日です。",
          "職場では、大きく褒められなくても、任された仕事を無理なく終えられそうです。チームで助け合い、落ち着いて働けるかもしれません。できたことを、同僚と一緒に確かめてください。"
        ]
      },
      "energy": {
        "text": [
          "体のケアがおろそかになり、疲れやすくなるかもしれません。食欲や気分に任せて食事を適当に済ませると、一日を過ごすのがさらに大変になりそうです。食べるのが面倒でも、食事を抜き続けるのは控えましょう。休んでも元気が戻らなかったり、不調が続いたりするなら、一人で我慢せず、診てもらってください。"
        ]
      }
    }
  }
}''')


@pytest.mark.parametrize("locale", ["ko", "ja", "en"])
async def test_current_writer_reads_saved_v8_prose_and_preserves_it_during_same_day_rebuild(
    enabled, monkeypatch, locale,
):
    profile = FortuneProfile(user_id=UID, gender="man", birth_date=date(2002, 12, 13), revision=1)
    session = _MemorySession(profile=profile)

    async def included(*_args, **_kwargs):
        return {"plan": "monthly", "entitlement_source": "subscription"}

    monkeypatch.setattr(fortune.gating, "resolve_entitlement", included)
    first = await fortune.reveal(session, str(UID), locale=locale, now_utc=NOW)
    assert first["versions"]["copy"] == fortune.fortune_catalog.COPY_VERSION
    writer_copy = fortune.fortune_catalog.render_cards_all(
        session.daily.semantic_result, selection=session.daily.semantic_result["copy_selection"],
    )[locale]
    assert first["result"]["overall"]["flow"] == writer_copy["overall"]["flow"]
    assert {axis: block["text"] for axis, block in first["result"]["categories"].items()} == {
        axis: block["text"] for axis, block in writer_copy["categories"].items()
    }
    assert session.daily.semantic_result["copy_selection"]["cards"] == {
        "overall": {"card_id": "swords.06", "orientation": "upright"},
        "love": {"card_id": "major.hanged_man", "orientation": "reversed"},
        "money": {"card_id": "swords.11", "orientation": "reversed"},
        "work": {"card_id": "wands.04", "orientation": "reversed"},
        "energy": {"card_id": "cups.01", "orientation": "reversed"},
    }
    for lang, copy in _V8_SAVED_COPY.items():
        session.daily.copy_by_locale[lang].update(deepcopy(copy))
    session.daily.copy_version = "fortune-copy.v8-localized.3"
    saved_copies = deepcopy(session.daily.copy_by_locale)
    saved_semantic = deepcopy(session.daily.semantic_result)
    saved_unlock = (session.daily.unlock_source, session.daily.unlocked_at)
    original_build = fortune._build_result
    rebuilds = []

    def record_build(**kwargs):
        rebuilds.append(kwargs)
        return original_build(**kwargs)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    async def database():
        yield session

    monkeypatch.setattr(fortune, "_build_result", record_build)
    monkeypatch.setattr(fortune, "datetime", Clock)
    app.dependency_overrides[get_current_user] = lambda: str(UID)
    app.dependency_overrides[get_session] = database
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"X-App-Locale": locale},
        ) as client:
            before_get = session.commits
            status = await client.get("/daily-fortune/status")
            assert status.status_code == 200
            assert session.commits == before_get and rebuilds == []
            assert status.json()["versions"]["copy"] == "fortune-copy.v8-localized.3"
            assert status.json()["result"]["overall"]["flow"] == _V8_SAVED_COPY[locale]["overall"]["flow"]
            assert len(status.json()["result"]["categories"]["love"]["text"]) == 3
            assert len(status.json()["result"]["categories"]["energy"]["text"]) == 1
            repeat = await client.post("/daily-fortune/reveal")
            assert repeat.status_code == 200 and rebuilds == []
            assert repeat.json()["result"] == status.json()["result"]
            assert session.daily.copy_by_locale == saved_copies
            assert session.daily.semantic_result == saved_semantic

            # A legacy response envelope can be rebuilt without replacing same-day copy.
            session.daily.result_schema_version = 2
            rebuilt = await client.post("/daily-fortune/reveal")
            assert rebuilt.status_code == 200 and len(rebuilds) == 1
            assert session.daily.result_schema_version == 3
            assert rebuilt.json()["result"] == status.json()["result"]
            assert session.daily.copy_by_locale == saved_copies
            assert session.daily.semantic_result == saved_semantic
            assert session.daily.copy_version == "fortune-copy.v8-localized.3"
            assert (session.daily.unlock_source, session.daily.unlocked_at) == saved_unlock
    finally:
        app.dependency_overrides.clear()

    # The next date uses the active local writer and keeps the same reader contract.
    tomorrow = await fortune.reveal(session, str(UID), locale=locale, now_utc=NOW + timedelta(days=1))
    assert tomorrow["versions"]["copy"] == fortune.fortune_catalog.COPY_VERSION
    writer_copy = fortune.fortune_catalog.render_cards_all(
        session.daily.semantic_result, selection=session.daily.semantic_result["copy_selection"],
    )[locale]
    assert tomorrow["result"]["overall"]["flow"] == writer_copy["overall"]["flow"]
    assert {axis: block["text"] for axis, block in tomorrow["result"]["categories"].items()} == {
        axis: block["text"] for axis, block in writer_copy["categories"].items()
    }
