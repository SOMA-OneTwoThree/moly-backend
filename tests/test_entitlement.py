"""티어 판정(entitlement) 순수 로직 — ERD §6.1. 각 티어·클램프·설정누락."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.entitlement import derive_entitlement

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
CONFIG = {
    "daily_token_limit": {"free": 1000, "trial": 5000, "subscriber": 5000},
    "diary_llm_min_tokens": 800,
}


def _profile(trial_ends_at):
    return SimpleNamespace(trial_ends_at=trial_ends_at)


def test_subscriber():
    sub = SimpleNamespace(plan="monthly")
    e = derive_entitlement(_profile(None), sub, 1200, CONFIG, NOW)
    assert e["plan"] == "monthly"
    assert e["is_subscriber"] is True
    assert e["ads_removed"] is True
    assert e["subscriber_theme_unlocked"] is True
    assert e["trial_ends_at"] is None
    assert e["daily_token_limit"] == 5000
    assert e["tokens_remaining"] == 3800


def test_trial():
    prof = _profile(NOW + timedelta(days=1))
    e = derive_entitlement(prof, None, 0, CONFIG, NOW)
    assert e["plan"] == "trial"
    assert e["is_subscriber"] is False
    assert e["ads_removed"] is True  # 체험도 광고 제거
    assert e["subscriber_theme_unlocked"] is False  # 체험은 구독 전용 테마 제외
    assert e["trial_ends_at"] == prof.trial_ends_at
    assert e["daily_token_limit"] == 5000  # trial = subscriber 수준


def test_free_when_trial_expired():
    e = derive_entitlement(_profile(NOW - timedelta(days=1)), None, 500, CONFIG, NOW)
    assert e["plan"] == "free"
    assert e["ads_removed"] is True  # 배너 광고 미출시 — 전 등급 항상 True(2026-07-09)
    assert e["subscriber_theme_unlocked"] is False
    assert e["daily_token_limit"] == 1000
    assert e["tokens_remaining"] == 500


def test_tokens_remaining_clamped_to_zero():
    e = derive_entitlement(_profile(None), None, 1500, CONFIG, NOW)  # free, used>limit
    assert e["plan"] == "free"
    assert e["tokens_remaining"] == 0


def test_missing_config_yields_nulls():
    e = derive_entitlement(_profile(None), None, 300, {}, NOW)
    assert e["daily_token_limit"] is None
    assert e["tokens_remaining"] is None
    assert e["personal_diary_token_threshold"] is None
