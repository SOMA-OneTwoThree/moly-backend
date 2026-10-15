"""토큰 한도·임계 해석 — app_config 값이 있으면 우선, 없으면 settings 임의 기본값(TBD).

daily_token_limit 은 {free,trial,subscriber} dict. entitlement/gating이 공유.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.config_store import get_config_values

_log = logging.getLogger("moly-backend")
_warned_missing_launch_row = False

CONFIG_KEYS = [
    "daily_token_limit",
    "diary_llm_min_tokens",
    "diary_min_user_chars",
    "review_prompt_min_tokens",
    "token_warning_threshold",
    "free_launch_until",
    "free_launch_token_limit",
]


async def effective_token_config(
    session: AsyncSession, *, raw: dict[str, Any] | None = None
) -> dict[str, Any]:
    # raw: 호출측이 app_config를 이미 읽어 왔으면 재조회하지 않는다(#11 — 요청당 왕복 병합).
    cfg = raw if raw is not None else await get_config_values(session, CONFIG_KEYS)
    if "free_launch_until" not in cfg:
        _warn_missing_launch_row()
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


def _warn_missing_launch_row() -> None:
    """행이 없으면 코드 기본값으로 떨어진다(의도된 폴백). 기본값이 이미 지난 날짜면 복구·신규 환경에서
    런칭 무료가 소리 없이 끝나므로, 동작은 그대로 두고 프로세스당 한 번 흔적만 남긴다."""
    global _warned_missing_launch_row
    if not _warned_missing_launch_row:
        _warned_missing_launch_row = True
        _log.warning(
            "app_config.free_launch_until 행 없음 → 코드 기본값 %s 사용(지난 날짜면 런칭 즉시 종료)",
            settings.free_launch_until,
        )
