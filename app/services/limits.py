"""토큰 한도·임계 해석 — app_config 값이 있으면 우선, 없으면 settings 임의 기본값(TBD).

daily_token_limit 은 {free,trial,subscriber} dict. entitlement/gating이 공유.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.config_store import get_config_values

_KEYS = [
    "daily_token_limit",
    "diary_llm_min_tokens",
    "diary_min_user_chars",
    "review_prompt_min_tokens",
    "token_warning_threshold",
    "free_launch_until",
    "free_launch_token_limit",
]


async def effective_token_config(session: AsyncSession) -> dict[str, Any]:
    cfg = await get_config_values(session, _KEYS)
    limits = cfg.get("daily_token_limit")
    if not isinstance(limits, dict):
        limits = {
            "free": settings.daily_token_limit_free,
            "trial": settings.daily_token_limit_trial,
            "subscriber": settings.daily_token_limit_subscriber,
        }
    warning_threshold = cfg.get("token_warning_threshold")
    if (
        not isinstance(warning_threshold, int)
        or isinstance(warning_threshold, bool)
        or warning_threshold < 0
    ):
        warning_threshold = settings.token_warning_threshold
    return {
        "daily_token_limit": limits,
        "diary_llm_min_tokens": cfg.get("diary_llm_min_tokens", settings.diary_llm_min_tokens),
        "diary_min_user_chars": cfg.get("diary_min_user_chars", settings.diary_min_user_chars),
        "review_prompt_min_tokens": cfg.get(
            "review_prompt_min_tokens", settings.review_prompt_min_tokens
        ),
        "token_warning_threshold": warning_threshold,
        "free_launch_until": cfg.get("free_launch_until", settings.free_launch_until),
        "free_launch_token_limit": cfg.get(
            "free_launch_token_limit", settings.free_launch_token_limit
        ),
    }
