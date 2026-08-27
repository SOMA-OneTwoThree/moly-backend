"""토큰 예산·티어 게이팅 — 대화 한도 판정의 단일 지점.

계정의 로더(프로필·구독·당일토큰) + entitlement 판정 + 한도 해석(app_config→임의기본값)을 조립.
chat의 사전 차단(DAILY_LIMIT_REACHED)·상태(/chat/state)가 이걸 사용.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.time_utils import activity_date_for
from app.models.profile import Profile
from app.services.account import (
    _load_active_subscription,
    _load_profile,
    _load_tokens_used,
)
from app.services.entitlement import derive_entitlement
from app.services.limits import effective_token_config


@dataclass
class Gating:
    profile: Profile
    activity_date: date
    entitlement: dict[str, Any]  # plan·tokens_remaining·daily_token_limit·personal_diary_token_threshold …
    tokens_used: int
    warning_threshold: int
    review_min_tokens: int
    # 개인 일기를 쓸지 정하는 기준 = 그날 **유저가 쓴 글자 수**다. 토큰이 아니다
    # (`diary_generation.generate_for_user`). 토큰 기준값 `diary_llm_min_tokens`는
    # 남아 있지만 실제 판정에는 쓰이지 않는다.
    # 기본값을 두는 이유는 이 값을 넘기지 않는 호출부(주로 테스트)를 깨지 않기 위함이다.
    diary_min_user_chars: int = settings.diary_min_user_chars


async def resolve(session: AsyncSession, user_id: str, now: datetime | None = None) -> Gating:
    now = now or datetime.now(timezone.utc)
    profile = await _load_profile(session, user_id)
    activity_date = activity_date_for(now, profile.timezone)
    sub = await _load_active_subscription(session, user_id, now)
    tokens_used = await _load_tokens_used(session, user_id, activity_date)
    cfg = await effective_token_config(session)
    entitlement = derive_entitlement(profile, sub, tokens_used, cfg, now)
    return Gating(
        profile=profile,
        activity_date=activity_date,
        entitlement=entitlement,
        tokens_used=tokens_used,
        warning_threshold=cfg["token_warning_threshold"],
        review_min_tokens=cfg["review_prompt_min_tokens"],
        diary_min_user_chars=cfg["diary_min_user_chars"],
    )


async def resolve_plan(
    session: AsyncSession,
    user_id: str,
    now: datetime,
    *,
    profile: Profile | None = None,
) -> str:
    """운세처럼 plan만 필요한 경로에서 토큰 사용량 조회 없이 등급을 판정한다."""

    loaded_profile = profile or await _load_profile(session, user_id)
    sub = await _load_active_subscription(session, user_id, now)
    cfg = await effective_token_config(session)
    return str(derive_entitlement(loaded_profile, sub, 0, cfg, now)["plan"])
