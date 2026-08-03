"""현재 턴 컨텍스트 — 챗 프롬프트에 삽입할 "지금 상황" 블록(DTO·버킷·렌더, DB 접근 없음).

DB 조회(프로필·장착 아이템·루틴 집계)는 이 모듈의 책임이 아니다 — 호출측(chat.py 배선 단계)이
값을 채운 CurrentTurnContext를 만들어 render()에 넘긴다. 여기는 순수 함수만 둔다(테스트 용이성).

주의: time_bucket()은 이 모듈 전용 4버킷(morning/day/evening/night)이다.
`app/services/greetings.py:time_bucket`은 동명이지만 선발화 전용 5버킷(dawn 분리)이라
용도가 다르다 — 그 함수는 건드리지 않는다(다른 담당 기능, 회귀 방지).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.time_utils import current_reward_date, safe_zone
from app.models.profile import Profile
from app.models.routine import Routine, RoutineCompletion
from app.models.user_device import UserDevice
from app.services import i18n, shop
from app.services.memory import _sanitize  # 렌더 경로 프롬프트 인젝션 방어(대괄호·제어문자 살균) 재사용

_log = logging.getLogger("moly-backend")


@dataclass(frozen=True)
class CurrentTurnContext:
    time_bucket: str | None = None        # "morning"|"day"|"evening"|"night"
    is_first_today: bool | None = None
    days_together: int | None = None
    equipped_names: list[str] = field(default_factory=list)
    theme_name: str | None = None
    routines_planned: int | None = None
    routines_done: int | None = None
    last_active_bucket: str | None = None  # "just_now"|"today"|"recent"|"long"


def time_bucket(hour: int) -> str:
    """로컬 시각(0~23) → 현재 턴 컨텍스트 시간대 4버킷. 하루 경계(04:00)에 맞춰 새벽부터 아침."""
    if 4 <= hour <= 10:
        return "morning"
    if 11 <= hour <= 16:
        return "day"
    if 17 <= hour <= 20:
        return "evening"
    return "night"  # 21~03


def last_active_bucket(delta_seconds: float) -> str:
    """마지막 활동 후 경과(초) → 4버킷. 음수(미래값·시계 오차)는 0으로 clamp하고 경고 로그."""
    if delta_seconds < 0:
        _log.warning("last_active_bucket: 음수 delta_seconds=%r → 0으로 clamp", delta_seconds)
        delta_seconds = 0
    if delta_seconds < 600:
        return "just_now"
    if delta_seconds < 86_400:
        return "today"
    if delta_seconds < 604_800:
        return "recent"
    return "long"


# --- 렌더 라벨(ko/ja/en) — i18n.pick 표. 하드코딩 언어분기 금지(SOMA-346 이후 규칙). ---
_LABEL_NOW = {"ko": "지금", "en": "Now", "ja": "いま"}
_LABEL_APPEARANCE = {"ko": "모습", "en": "Look", "ja": "すがた"}
_LABEL_ROUTINE = {"ko": "루틴", "en": "Routines", "ja": "ルーティン"}

_TIME_BUCKET_TEXT = {
    "morning": {"ko": "아침", "en": "morning", "ja": "朝"},
    "day": {"ko": "낮", "en": "day", "ja": "昼"},
    "evening": {"ko": "저녁", "en": "evening", "ja": "夕方"},
    "night": {"ko": "밤", "en": "night", "ja": "夜"},
}
_LAST_ACTIVE_TEXT = {
    "just_now": {"ko": "방금 옴", "en": "just arrived", "ja": "たった今来た"},
    "today": {"ko": "오늘 다녀감", "en": "active today", "ja": "今日活動あり"},
    "recent": {"ko": "최근에 다녀감", "en": "active recently", "ja": "最近活動あり"},
    "long": {"ko": "오랜만에 옴", "en": "long time no see", "ja": "久しぶりに来た"},
}
_FIRST_TODAY = {"ko": "오늘 첫 대화", "en": "first chat today", "ja": "今日最初の会話"}
_DAYS_TOGETHER = {"ko": "함께한 지 {days}일", "en": "{days} days together", "ja": "一緒に{days}日"}
_ROOM_PREFIX = {"ko": "방: {name}", "en": "Room: {name}", "ja": "部屋: {name}"}
_ROUTINE_TEXT = {
    "ko": "오늘 예정 {planned}개 중 {done}개 완료",
    "en": "{done}/{planned} routines done today",
    "ja": "今日の予定{planned}件中{done}件完了",
}


def render(ctx: CurrentTurnContext, language: str | None) -> str:
    """DTO → 프롬프트 블록 문자열. 값이 없는 항목은 조각을 생략하고, 줄 전체가 비면 그 줄 자체를 뺀다."""
    lines: list[str] = []

    now_parts: list[str] = []
    bucket_table = _TIME_BUCKET_TEXT.get(ctx.time_bucket) if ctx.time_bucket else None
    if bucket_table:
        now_parts.append(i18n.pick(bucket_table, language))
    if ctx.is_first_today:
        now_parts.append(i18n.pick(_FIRST_TODAY, language))
    if ctx.days_together is not None:
        now_parts.append(i18n.pick(_DAYS_TOGETHER, language).format(days=ctx.days_together))
    last_active_table = _LAST_ACTIVE_TEXT.get(ctx.last_active_bucket) if ctx.last_active_bucket else None
    if last_active_table:
        now_parts.append(i18n.pick(last_active_table, language))
    if now_parts:
        lines.append(f"[{i18n.pick(_LABEL_NOW, language)}] " + " · ".join(now_parts))

    look_parts: list[str] = []
    sanitized_items = [s for name in ctx.equipped_names if (s := _sanitize(name))]
    if sanitized_items:
        look_parts.append(" · ".join(sanitized_items))
    if ctx.theme_name:
        theme = _sanitize(ctx.theme_name)
        if theme:
            look_parts.append(i18n.pick(_ROOM_PREFIX, language).format(name=theme))
    if look_parts:
        lines.append(f"[{i18n.pick(_LABEL_APPEARANCE, language)}] " + " · ".join(look_parts))

    if ctx.routines_planned is not None:  # 이름 금지 — 숫자만 렌더
        done = ctx.routines_done or 0
        routine_text = i18n.pick(_ROUTINE_TEXT, language).format(planned=ctx.routines_planned, done=done)
        lines.append(f"[{i18n.pick(_LABEL_ROUTINE, language)}] " + routine_text)

    return "\n".join(lines)
