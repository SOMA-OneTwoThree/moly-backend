"""티어(entitlement) 판정 — ERD §6.1. 컬럼 저장 아님, 조회 시 판정.

핵심 로직은 순수 함수 `derive_entitlement`(DB 무관 → 단위테스트). DB 로드는 account 서비스.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol


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
    # 실제 구독자(active_sub)는 항상 우선 — 증정 등 정상. 기간 지나면 자동으로 정상 등급 복귀.
    launch_until = _parse_dt(config.get("free_launch_until"))
    in_launch = active_sub is None and launch_until is not None and now < launch_until

    if active_sub is not None:
        plan = active_sub.plan  # monthly | yearly
        is_subscriber = True
        trial_ends_at = None
        subscriber_theme_unlocked = True
    elif in_launch:
        # plan은 클라 호환 위해 'trial' 재사용(새 값 도입 안 함). trial_ends_at=런칭 종료로 "무료 ~까지" 표시.
        plan = "trial"
        is_subscriber = False
        trial_ends_at = launch_until
        subscriber_theme_unlocked = False
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
    if in_launch:
        # 런칭 전용 한도(구독 100k와 독립). 값 없으면 trial 수준으로 fail-safe.
        ll = config.get("free_launch_token_limit")
        limit = ll if isinstance(ll, int) else _limit_for("trial", limits if isinstance(limits, dict) else {})
    else:
        limit = _limit_for(plan, limits) if isinstance(limits, dict) else None
    tokens_remaining = max(0, limit - tokens_used) if limit is not None else None
    threshold = config.get("diary_llm_min_tokens")

    return {
        "plan": plan,
        "is_subscriber": is_subscriber,
        "trial_ends_at": trial_ends_at,
        # 배너 광고 미출시 결정(2026-07-09) — 항상 True. 도입 시 plan != "free"로 복원.
        "ads_removed": True,
        "subscriber_theme_unlocked": subscriber_theme_unlocked,  # 구독만(체험 제외)
        "daily_token_limit": limit,
        "tokens_used": tokens_used,
        "tokens_remaining": tokens_remaining,
        "personal_diary_token_threshold": threshold if isinstance(threshold, int) else None,
    }
