"""티어(entitlement) 판정 — ERD §6.1. 컬럼 저장 아님, 조회 시 판정.

핵심 로직은 순수 함수 `derive_entitlement`(DB 무관 → 단위테스트). DB 로드는 account 서비스.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class _ProfileLike(Protocol):
    trial_ends_at: datetime | None


class _SubLike(Protocol):
    plan: str  # monthly | yearly


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
    if active_sub is not None:
        plan = active_sub.plan  # monthly | yearly
        is_subscriber = True
        trial_ends_at = None
        subscriber_theme_unlocked = True
    elif profile.trial_ends_at is not None and now < profile.trial_ends_at:
        plan = "trial"
        is_subscriber = False
        trial_ends_at = profile.trial_ends_at
        subscriber_theme_unlocked = False
    else:
        plan = "free"
        is_subscriber = False
        trial_ends_at = None
        subscriber_theme_unlocked = False

    limits = config.get("daily_token_limit") or {}
    limit = _limit_for(plan, limits) if isinstance(limits, dict) else None
    tokens_remaining = max(0, limit - tokens_used) if limit is not None else None
    threshold = config.get("diary_llm_min_tokens")

    return {
        "plan": plan,
        "is_subscriber": is_subscriber,
        "trial_ends_at": trial_ends_at,
        "ads_removed": plan != "free",  # free만 배너 노출
        "subscriber_theme_unlocked": subscriber_theme_unlocked,  # 구독만(체험 제외)
        "daily_token_limit": limit,
        "tokens_used": tokens_used,
        "tokens_remaining": tokens_remaining,
        "personal_diary_token_threshold": threshold if isinstance(threshold, int) else None,
    }
