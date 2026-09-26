"""티어(entitlement) 판정 — ERD §6.1. 컬럼 저장 아님, 조회 시 판정.

핵심 로직은 순수 함수 `derive_entitlement`(DB 무관 → 단위테스트). DB 로드는 account 서비스.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.services.i18n import resolve as resolve_language


# Daily weighted quota units. Trials receive the same allowance as paid plans.
_ROLLOUT_LIMITS = {
    "en": {"free": 40_000, "subscriber": 300_000},
    "ko": {"free": 50_000, "subscriber": 400_000},
    "ja": {"free": 60_000, "subscriber": 550_000},
}


class _ProfileLike(Protocol):
    trial_ends_at: datetime | None


class _SubLike(Protocol):
    plan: str  # monthly | yearly


def _parse_dt(value: Any) -> datetime | None:
    """ISO8601(오프셋 포함 권장) → aware datetime. 미설정/파싱실패 = None(런칭 OFF, fail-safe).

    naive면 UTC로 간주(비교 크래시 방지). 잘못된 값이 '영구 무료'로 새지 않게 항상 안전 폴백.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def launch_ended(config: dict[str, Any], now: datetime) -> bool:
    """Subscription policy starts at free_launch_until; unset or invalid values end launch (fail-safe)."""
    launch_until = _parse_dt(config.get("free_launch_until"))
    return launch_until is None or now >= launch_until


def _limit_for(plan: str, limits: dict[str, Any]) -> int | None:
    """티어별 일 토큰 한도. trial은 subscriber와 동일 수준(ERD §6.1)."""
    if plan == "free":
        v = limits.get("free")
    elif plan == "trial":
        v = limits.get("trial", limits.get("subscriber"))
    else:  # monthly | yearly
        v = limits.get("subscriber")
    return v if isinstance(v, int) else None


def derive_entitlement(
    profile: _ProfileLike,
    active_sub: _SubLike | None,
    tokens_used: int,
    config: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """entitlement 블록 생성. active_sub는 '유효한(active/grace + 미만료)' 구독만 넘어옴(없으면 None)."""
    # 런칭 무료 기간: 구독 없이 전원 무료(구독급 경험, 단 토큰은 별도 런칭 한도).
    # 실제 구독자(active_sub)는 항상 우선. 앱 체험은 새 앱에서만 시작하므로 시작한 계정은 체험 정책을 따른다.
    launch_until = _parse_dt(config.get("free_launch_until"))
    has_app_trial = getattr(profile, "app_trial_started_at", None) is not None
    in_launch = (
        active_sub is None and not has_app_trial
        and launch_until is not None and now < launch_until
    )
    personal_trial_end = (
        getattr(profile, "app_trial_ends_at", None) if has_app_trial else profile.trial_ends_at
    )
    # bootstrap_user writes the original signup + 48h once, including self-heal.
    # Accounts created before launch receive the store offer, not a signup trial.
    if (
        not has_app_trial and personal_trial_end is not None and launch_until is not None
        and personal_trial_end - timedelta(hours=48) < launch_until
    ):
        personal_trial_end = None

    if active_sub is not None:
        plan = active_sub.plan  # monthly | yearly
        is_subscriber = True
        store_trial_end = getattr(active_sub, "store_trial_ends_at", None)
        trial_ends_at = store_trial_end if store_trial_end and now < store_trial_end else None
        source = "store_trial" if trial_ends_at is not None else "subscription"
        subscriber_theme_unlocked = True
    elif in_launch:
        # plan은 클라 호환 위해 'trial' 재사용(새 값 도입 안 함). trial_ends_at=런칭 종료로 "무료 ~까지" 표시.
        plan = "trial"
        is_subscriber = False
        trial_ends_at = launch_until
        source = "launch"
        subscriber_theme_unlocked = False
    elif personal_trial_end is not None and now < personal_trial_end:
        plan = "trial"
        is_subscriber = False
        trial_ends_at = personal_trial_end
        source = "signup_trial"
        subscriber_theme_unlocked = has_app_trial
    else:
        plan = "free"
        is_subscriber = False
        trial_ends_at = None
        source = "free"
        subscriber_theme_unlocked = False

    if in_launch:
        # 런칭 전용 한도(구독 한도와 독립). 값 없으면 trial 수준으로 fail-safe.
        ll = config.get("free_launch_token_limit")
        limits = config.get("daily_token_limit")
        limit = ll if isinstance(ll, int) else _limit_for("trial", limits if isinstance(limits, dict) else {})
    else:
        # Use persisted profile language, never a per-request locale/header.
        language_limits = _ROLLOUT_LIMITS[resolve_language(getattr(profile, "language", None))]
        limit = language_limits["free" if plan == "free" else "subscriber"]
    tokens_remaining = max(0, limit - tokens_used) if limit is not None else None
    threshold = config.get("diary_llm_min_tokens")

    return {
        "entitlement_source": source,
        "personal_diary_eligible": plan != "free",
        "plan": plan,
        "is_subscriber": is_subscriber,
        "trial_ends_at": trial_ends_at,
        "ads_removed": plan != "free",
        "subscriber_theme_unlocked": subscriber_theme_unlocked,
        "daily_token_limit": limit,
        "tokens_used": tokens_used,
        "tokens_remaining": tokens_remaining,
        "personal_diary_token_threshold": threshold if isinstance(threshold, int) else None,
    }
