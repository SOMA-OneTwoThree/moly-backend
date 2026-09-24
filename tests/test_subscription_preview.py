from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.services.entitlement import derive_entitlement
from app.services.limits import effective_token_config
from app.services import diary_generation, fortune, gating

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)
UID = uuid4()


def config(mode="regular"):
    return {
        "subscription_launch": {"enabled": False, "campaign_id": "live"},
        "subscription_launch_test": {
            "accounts": {str(UID): mode}, "campaign_id": "preview",
            "expires_at": "2026-09-29T15:00:00Z",
        },
        "free_launch_token_limit": 150_000,
    }


def profile(**kwargs):
    return SimpleNamespace(id=UID, language="ko", trial_ends_at=NOW + timedelta(days=100), **kwargs)


@pytest.mark.parametrize("mode", ["regular", "legacy_offer"])
def test_preview_uses_issued_trial_and_language_quota_without_resetting_usage(mode):
    cfg = config(mode)
    p = profile()
    before = derive_entitlement(p, None, 12_000, cfg, NOW)
    assert (before["plan"], before["daily_token_limit"], before["ads_removed"]) == ("free", 50_000, False)
    p.app_trial_started_at = NOW
    p.app_trial_ends_at = NOW + timedelta(hours=48)
    during = derive_entitlement(p, None, 12_000, cfg, NOW)
    assert (during["plan"], during["daily_token_limit"], during["subscriber_theme_unlocked"]) == ("trial", 400_000, True)
    assert during["ads_removed"] and during["personal_diary_eligible"]
    after = derive_entitlement(p, None, 12_000, cfg, p.app_trial_ends_at)
    assert after["plan"] == "free" and not after["personal_diary_eligible"]
    assert before["tokens_used"] == during["tokens_used"] == after["tokens_used"] == 12_000


@pytest.mark.parametrize("expiry", ["2026-09-24T00:00:00Z", "invalid", "infinity", "2026-09-29", "2026-09-29T15:00:00+09:00"])
def test_invalid_or_expired_preview_retains_production_launch(expiry):
    cfg = config()
    cfg["subscription_launch_test"]["expires_at"] = expiry
    assert derive_entitlement(profile(), None, 0, cfg, NOW)["daily_token_limit"] == 150_000


@pytest.mark.parametrize("rollout", [None, {}, {"enabled": True}, {"enabled": True, "existing_user_cutoff": "2026-10-01T00:00:00Z"}])
def test_preview_requires_explicitly_disabled_production(rollout):
    cfg = config()
    cfg["subscription_launch"] = rollout
    without = {**cfg, "subscription_launch_test": None}
    assert derive_entitlement(profile(), None, 0, cfg, NOW) == derive_entitlement(profile(), None, 0, without, NOW)


def test_unlisted_user_and_reused_legacy_campaign_keep_launch():
    cfg = config("legacy_offer")
    p = profile()
    p.id = uuid4()
    assert derive_entitlement(p, None, 0, cfg, NOW)["daily_token_limit"] == 150_000
    cfg["subscription_launch_test"]["campaign_id"] = "live"
    assert derive_entitlement(profile(), None, 0, cfg, NOW)["daily_token_limit"] == 150_000


@pytest.mark.parametrize("campaign", [None, "", 123, True, {}, []])
def test_legacy_preview_requires_a_string_campaign(campaign):
    cfg = config("legacy_offer")
    cfg["subscription_launch_test"]["campaign_id"] = campaign
    assert derive_entitlement(profile(), None, 0, cfg, NOW)["daily_token_limit"] == 150_000


async def test_effective_config_retains_preview_for_chat_and_worker():
    cfg = config()
    assert (await effective_token_config(None, raw=cfg))["subscription_launch_test"] == cfg["subscription_launch_test"]


@pytest.mark.parametrize("store_trial", [False, True])
def test_actual_subscription_takes_priority_in_preview(store_trial):
    sub = SimpleNamespace(plan="yearly", store_trial_ends_at=NOW + timedelta(days=30) if store_trial else None)
    result = derive_entitlement(profile(), sub, 12_000, config(), NOW)
    assert result["plan"] == "yearly" and result["is_subscriber"]
    assert result["daily_token_limit"] == 400_000 and result["ads_removed"]
    assert result["entitlement_source"] == ("store_trial" if store_trial else "subscription")


async def test_real_chat_gating_uses_preview_quota_and_keeps_counter(monkeypatch):
    p = profile(timezone="Asia/Seoul", app_trial_started_at=NOW, app_trial_ends_at=NOW + timedelta(hours=48))
    monkeypatch.setattr(gating, "_load_profile", AsyncMock(return_value=p))
    monkeypatch.setattr(gating, "_load_active_subscription", AsyncMock(return_value=None))
    monkeypatch.setattr(gating, "_load_tokens_used", AsyncMock(return_value=60_000))
    result = await gating.resolve(None, str(UID), NOW, config_raw=config())
    assert result.entitlement["daily_token_limit"] == 400_000
    assert result.entitlement["tokens_remaining"] == 340_000
    assert result.entitlement["ads_removed"]


async def test_preview_trial_fortune_is_included(monkeypatch):
    monkeypatch.setattr(gating, "resolve_plan", AsyncMock(return_value="trial"))
    monkeypatch.setattr(fortune, "effective_token_config", AsyncMock(return_value=config()))
    assert await fortune._access(None, str(UID), now=NOW, daily=None, today=NOW.date()) == ("included", "trial")
    assert await fortune._access(None, str(uuid4()), now=NOW, daily=None, today=NOW.date()) == ("ad_required", "trial")


async def test_preview_free_diary_is_not_launch_exempt():
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))
    p = profile(timezone="UTC")
    assert not await diary_generation._personal_diary_eligible(session, p, NOW.date(), config())
    session.execute.assert_awaited_once()
