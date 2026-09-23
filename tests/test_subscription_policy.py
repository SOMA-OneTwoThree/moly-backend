"""Rollout boundaries, unchanged counters and personal-diary eligibility."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.entitlement import derive_entitlement, subscription_policy_active
from app.services import diary_generation as dg
from tests.test_diary_generation import CFG, PROFILE, FakeSession, _msg, _patch_common

T = datetime(2026, 10, 1, 0, tzinfo=timezone.utc)
CONFIG = {
    "subscription_launch": {"enabled": True, "existing_user_cutoff": T.isoformat()},
    "free_launch_until": "2026-11-01T00:00:00Z", "free_launch_token_limit": 150_000,
    "daily_token_limit": {"free": 20_000, "trial": 100_000, "subscriber": 100_000},
}


def profile(**values):
    return SimpleNamespace(trial_ends_at=None, **values)


def test_transition_does_not_reset_usage_or_wait_for_client_trial_request():
    before = derive_entitlement(profile(), None, 120_000, CONFIG, T - timedelta(microseconds=1))
    after = derive_entitlement(profile(), None, 120_000, CONFIG, T)
    assert before["daily_token_limit"] == 150_000 and before["entitlement_source"] == "launch"
    assert after["daily_token_limit"] == 40_000 and after["entitlement_source"] == "free"
    assert before["tokens_used"] == after["tokens_used"] == 120_000
    assert after["tokens_remaining"] == 0 and not after["personal_diary_eligible"]


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_signup_cohort_boundary(offset):
    p = profile()
    p.trial_ends_at = T + timedelta(hours=48, microseconds=offset)
    e = derive_entitlement(p, None, 50_000, CONFIG, T + timedelta(hours=1))
    assert e["daily_token_limit"] == (40_000 if offset < 0 else 300_000)


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_signup_48h_expiry(offset):
    p = profile()
    p.trial_ends_at = T + timedelta(hours=48)
    e = derive_entitlement(p, None, 20_000, CONFIG, p.trial_ends_at + timedelta(microseconds=offset))
    assert e["plan"] == ("trial" if offset < 0 else "free")
    assert e["tokens_used"] == 20_000


@pytest.mark.parametrize("plan", ["monthly", "yearly"])
@pytest.mark.parametrize("trial", [False, True])
def test_store_and_paid_subscription_share_allowance_but_not_source(plan, trial):
    sub = SimpleNamespace(plan=plan, store_trial_ends_at=T + timedelta(days=30) if trial else None)
    e = derive_entitlement(profile(), sub, 80_000, CONFIG, T)
    assert e["daily_token_limit"] == 300_000 and e["tokens_remaining"] == 220_000
    assert e["entitlement_source"] == ("store_trial" if trial else "subscription")
    assert e["personal_diary_eligible"]


@pytest.mark.parametrize("rollout", [None, {}, {"enabled": False, "existing_user_cutoff": T.isoformat()},
    {"enabled": True, "existing_user_cutoff": "invalid"},
    {"enabled": True, "existing_user_cutoff": "2026-09-01T00:00:00"}])
def test_invalid_or_disabled_rollout_keeps_old_policy(rollout):
    cfg = {**CONFIG, "subscription_launch": rollout}
    assert not subscription_policy_active(cfg, T)
    assert derive_entitlement(profile(), None, 0, cfg, T)["daily_token_limit"] == 150_000


@pytest.mark.parametrize("language,free,paid", [
    ("en", 40_000, 300_000), ("ko", 50_000, 400_000), ("ja", 60_000, 550_000),
    ("ko-KR", 50_000, 400_000), ("ja-JP", 60_000, 550_000),
    ("EN-us", 40_000, 300_000), (None, 40_000, 300_000), ("zh", 40_000, 300_000),
])
@pytest.mark.parametrize("source", ["free", "signup", "app_trial", "monthly", "yearly", "store_trial"])
def test_language_allowances_preserve_usage_and_trial_benefits(language, free, paid, source):
    p = profile(language=language)
    sub = None
    if source == "signup":
        p.trial_ends_at = T + timedelta(hours=48)
    elif source == "app_trial":
        p.app_trial_started_at = T
        p.app_trial_ends_at = T + timedelta(hours=48)
    elif source in {"monthly", "yearly", "store_trial"}:
        sub = SimpleNamespace(plan="yearly" if source == "yearly" else "monthly",
                              store_trial_ends_at=T + timedelta(days=30) if source == "store_trial" else None)
    result = derive_entitlement(p, sub, 25_000, CONFIG, T)
    limit = free if source == "free" else paid
    assert result["daily_token_limit"] == limit
    assert result["tokens_used"] == 25_000
    assert result["tokens_remaining"] == limit - 25_000
    assert result["ads_removed"] == (source != "free")


def test_language_change_and_subscription_expiry_never_reset_daily_counter():
    p = profile(language="en")
    for language, expected_remaining in [("en", 0), ("ja", 15_000), ("ko", 5_000), ("en", 0)]:
        p.language = language
        e = derive_entitlement(p, None, 45_000, CONFIG, T)
        assert e["tokens_used"] == 45_000 and e["tokens_remaining"] == expected_remaining
    p.language = "ja"
    sub = SimpleNamespace(plan="monthly")
    assert derive_entitlement(p, sub, 70_000, CONFIG, T)["tokens_remaining"] == 480_000
    assert derive_entitlement(p, None, 70_000, CONFIG, T)["tokens_remaining"] == 0


@pytest.mark.parametrize("language", ["en", "ko", "ja"])
def test_scheduled_cutoff_does_not_change_launch_allowance(language):
    e = derive_entitlement(profile(language=language), None, 90_000, CONFIG, T - timedelta(seconds=1))
    assert e["daily_token_limit"] == 150_000 and e["tokens_remaining"] == 60_000


@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("prior_trial", [False, True])
def test_prepared_release_cannot_expire_launch_before_app_release(scheduled, prior_trial):
    cfg = {**CONFIG, "subscription_launch": {"enabled": scheduled, "existing_user_cutoff": T.isoformat()},
           "free_launch_until": (T - timedelta(days=30)).isoformat()}
    p = profile(language="ja")
    if prior_trial:
        p.app_trial_started_at = T - timedelta(days=40)
        p.app_trial_ends_at = T - timedelta(days=38)
    result = derive_entitlement(p, None, 12_000, cfg, T - timedelta(seconds=1))
    assert result["entitlement_source"] == "launch"
    assert result["daily_token_limit"] == 150_000
    assert result["tokens_remaining"] == 138_000 and result["personal_diary_eligible"]
    assert result["trial_ends_at"] == (T if scheduled else None)


async def test_free_user_gets_operator_diary_without_personal_llm(monkeypatch):
    _patch_common(monkeypatch, messages=[_msg("user", "대화를 충분히 많이 했어")],
                  ment=SimpleNamespace(id=1, content="오늘은 캐피의 하루", weather="sunny"))
    monkeypatch.setattr(dg, "_personal_diary_eligible", AsyncMock(return_value=False))
    personal = AsyncMock()
    monkeypatch.setattr(dg, "_personal", personal)
    session = FakeSession()
    result = await dg.generate_for_user(session, PROFILE, date(2026, 10, 2), CFG, policy=dg.DiaryPolicy())
    personal.assert_not_awaited()
    assert result["gate_passed"] and not result["personal_attempted"]
    assert session.added[0].source == "preset"


async def test_previous_day_keeps_original_policy_without_subscription_lookup():
    session = AsyncMock()
    assert await dg._personal_diary_eligible(session, PROFILE, date(2026, 9, 29), CONFIG)
    session.execute.assert_not_awaited()


async def test_delayed_worker_uses_completed_day_not_wall_clock():
    p = SimpleNamespace(**vars(PROFILE), trial_ends_at=T + timedelta(hours=48))
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))
    assert await dg._personal_diary_eligible(session, p, date(2026, 10, 1), CONFIG)
    assert not await dg._personal_diary_eligible(session, p, date(2026, 10, 3), CONFIG)
    query = str(session.execute.call_args.args[0])
    assert "subscriptions.purchased_at <=" in query and "subscriptions.expires_at >" in query
