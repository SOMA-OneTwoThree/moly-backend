"""현재 턴 컨텍스트 — 챗 프롬프트에 삽입할 "지금 상황" 블록(수집·버킷·렌더).

구성은 세 층이다. `CurrentTurnContext`(DTO) / `build_context`(DB 조회로 DTO를 채운다) /
`render`·`time_bucket`·`last_active_bucket`(DTO만 받는 순수 함수). 조회와 렌더를 나눠 둔 덕에
문구·경계 테스트는 DB 없이 DTO만 만들어 돌릴 수 있다.

호출은 chat.py의 Phase 1 안에서만 한다 — LLM 구간엔 DB 커넥션이 0이어야 하기 때문(SOMA-374).

주의: time_bucket()은 이 모듈 전용 4버킷(morning/day/evening/night)이다.
`app/services/greetings.py:time_bucket`은 동명이지만 선발화 전용 5버킷(dawn 분리)이라
용도가 다르다 — 그 함수는 건드리지 않는다(다른 담당 기능, 회귀 방지).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.time_utils import reward_date_for, safe_zone
from app.models.profile import Profile
from app.models.routine import Routine, RoutineCompletion
from app.models.chat_context import ChatContext
from app.services import i18n, shop
# 대괄호·제어문자 제거. 상품명은 우리 카탈로그 값이지만 기억 렌더와 같은 살균을 통과시킨다.
# ⚠️ 이건 표현 정리이지 보안 경계가 아니다 — 이걸 믿고 신뢰할 수 없는 입력을 넣지 마라.
from app.services.memory import _sanitize

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


async def build_context(
    session: AsyncSession,
    profile: Profile,
    *,
    is_first_today: bool,
    now_utc: datetime,
) -> CurrentTurnContext:
    """DB 조회로 채운 CurrentTurnContext(chat.py 배선 단계가 호출 → render()에 전달).

    조회 3개(장착 아이템·루틴 집계·마지막 활동)는 각각 `session.begin_nested()` savepoint로
    감싼다 — 이 블록은 항목별 fail-open이어야 한다(한 조회가 죽어도 나머지 문구는 살려야
    프롬프트에 빈 블록만 빠지고 캐피 발화 전체가 죽지 않는다). 단일 배치 SQL로 묶으면
    subquery 하나의 실패가 전체를 무효화해 이 fail-open을 못 만든다.
    ⚠️ 이 레포의 다른 begin_nested는 전부 "동시 INSERT 충돌을 멱등하게 수렴"시키려는 것이지만
    여기는 "조회 실패 격리"라는 다른 용도다 — 재시도할 동시성 문제가 아니라, 그 필드만 비우고
    넘어가려고 SAVEPOINT로 트랜잭션 오염을 막는다. 실패 시 해당 필드는 None(리스트는 빈 리스트).
    """
    uid = profile.id

    equipped_names: list[str] = []
    theme_name: str | None = None
    try:
        async with session.begin_nested():
            rows = await shop._user_rows(session, uid)
            equipped_rows = [r for r in rows if r.equipped_slot is not None]
            # _user_rows에 ORDER BY가 없어 DB 반환 순서가 실행마다 달라질 수 있다 — 프롬프트에
            # 박히는 문자열이라 재현성이 필요하므로 shop._SLOTS_V2 표준 슬롯 순서로 고정 정렬한다.
            equipped_rows.sort(
                key=lambda r: (
                    shop._SLOTS_V2.index(r.equipped_slot)
                    if r.equipped_slot in shop._SLOTS_V2
                    else len(shop._SLOTS_V2)
                )
            )
            products = await shop._products_by_ids(
                session, {r.product_id for r in equipped_rows}
            )
            names: list[str] = []
            theme: str | None = None
            for row in equipped_rows:
                product = products.get(row.product_id)
                if product is None:
                    continue  # 비활성화·삭제된 상품 — 조용히 생략(이미 착용 목록엔 안 남아야 정상)
                name = i18n.localized_name(
                    product.name_i18n, profile.language, product.name,
                    kind="product", key=str(product.id),
                )
                if row.equipped_slot == "theme":
                    theme = name
                else:
                    names.append(name)
            equipped_names, theme_name = names, theme
    except Exception:
        _log.warning("build_context: 장착 아이템 조회 실패 — 해당 필드 생략", exc_info=True)
        equipped_names, theme_name = [], None

    routines_planned: int | None = None
    routines_done: int | None = None
    try:
        async with session.begin_nested():
            # 루틴의 "오늘" = 00:00 경계(대화의 activity_date 04:00과 다르다) — 팀장 결정이자
            # 기존 루틴 도메인의 진실(routine.py:13,54가 이 기준을 쓴다). 유저가 앱에서 보는 루틴
            # 완료 상태와 캐피 발화가 어긋나면 안 되기 때문. now_utc를 넘겨야 한다(현재 시각을
            # 내부에서 부르는 current_reward_date를 쓰면 time_bucket·days_together는 now_utc를
            # 따르는데 루틴만 실제 벽시계를 따라 비일관·시간의존 테스트가 된다).
            ad = reward_date_for(now_utc, profile.timezone)
            weekday = ad.isoweekday()  # ISO 1=월 ~ 7=일
            all_routines = list(
                (
                    await session.execute(
                        select(Routine).where(
                            Routine.user_id == uid, Routine.deleted_at.is_(None)
                        )
                    )
                ).scalars().all()
            )
            planned_ids = {r.id for r in all_routines if weekday in r.days_of_week}
            done_ids = set(
                (
                    await session.execute(
                        select(RoutineCompletion.routine_id).where(
                            RoutineCompletion.user_id == uid,
                            RoutineCompletion.activity_date == ad,
                        )
                    )
                ).scalars().all()
            )
            routines_planned = len(planned_ids)
            routines_done = len(done_ids & planned_ids)
    except Exception:
        _log.warning("build_context: 루틴 집계 조회 실패 — 해당 필드 생략", exc_info=True)
        routines_planned, routines_done = None, None

    last_active_bucket_value: str | None = None
    if settings.current_context_last_active_enabled:
        try:
            async with session.begin_nested():
                last_active_at = (
                    await session.execute(
                        select(ChatContext.last_active_at).where(ChatContext.user_id == uid)
                    )
                ).scalar()
                if last_active_at is not None:
                    delta = (now_utc - last_active_at).total_seconds()
                    last_active_bucket_value = last_active_bucket(delta)
        except Exception:
            _log.warning("build_context: 마지막 활동 조회 실패 — 해당 필드 생략", exc_info=True)
            last_active_bucket_value = None

    # 로컬 날짜끼리 빼야 한다 — UTC 날짜로 빼면 유저 로컬 자정~09시(KST) 가입처럼 UTC 전날로
    # 넘어가는 경우 하루가 더 늘어난다("함께한 지 N일"이 유저 체감보다 1 크게 보임).
    zone = safe_zone(profile.timezone)
    local_now = now_utc.astimezone(zone)
    days_together = (
        (local_now.date() - profile.created_at.astimezone(zone).date()).days
        if profile.created_at is not None
        else None
    )
    if days_together is not None and days_together < 0:
        # 미래 created_at(시계 오차 등)이면 음수가 나올 수 있다 — last_active_bucket과 동일하게
        # 0으로 clamp하고 경고(음수 "함께한 지 -1일"을 그대로 렌더하지 않는다).
        _log.warning("build_context: 미래 created_at → days_together=%r을 0으로 clamp", days_together)
        days_together = 0

    return CurrentTurnContext(
        time_bucket=time_bucket(local_now.hour),
        is_first_today=is_first_today,
        days_together=days_together,
        equipped_names=equipped_names,
        theme_name=theme_name,
        routines_planned=routines_planned,
        routines_done=routines_done,
        last_active_bucket=last_active_bucket_value,
    )


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
# ⚠️ 루틴은 **상대가 하는 일**이다. 라벨을 그냥 "루틴"으로 두면 캐피가 자기 할 일로 읽고
# "아직 루틴을 하나도 못 했다"처럼 말한다(실제 발생). 소유를 라벨에 박아 넣는다.
_LABEL_ROUTINE = {"ko": "상대 루틴", "en": "Their routines", "ja": "相手のルーティン"}

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
