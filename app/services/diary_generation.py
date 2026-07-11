"""일기 생성 배치 로직 — 워커가 04:00 틱에 전일 일기를 만든다.

분기(ERD §5.3): 전일 누적토큰 ≥ 임계 → 개인(llm, Sonnet 생성 + Haiku self-check)
              / 미달·미접속 → 캐피(preset, 멘트 풀). 멱등: unique(user, diary_date).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.diary import Diary
from app.models.message import Message
from app.models.moly_life_ment import MolyLifeMent
from app.models.user_daily_stats import UserDailyStats
from app.services import llm, memory
from app.services.diary_prompts import diary_prompt, parse, self_check_prompt

_log = logging.getLogger("moly-worker")


def publish_at(target_date: date, tz_name: str) -> datetime:
    """전일(target_date) 일기 발행 = 익일 로컬 09:00 → UTC."""
    local = datetime.combine(target_date + timedelta(days=1), time(9, 0), tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


async def _diary_exists(session: AsyncSession, user_id, target_date: date) -> bool:
    row = await session.execute(
        select(Diary.id).where(Diary.user_id == user_id, Diary.diary_date == target_date)
    )
    return row.scalars().first() is not None


async def _day_messages(session: AsyncSession, user_id, target_date: date) -> list[Message]:
    rows = await session.execute(
        select(Message)
        .where(
            Message.user_id == user_id,
            Message.activity_date == target_date,
            Message.kind == "normal",
        )
        .order_by(Message.id.asc())
    )
    return list(rows.scalars().all())


async def _tokens_used(session: AsyncSession, user_id, target_date: date) -> int:
    rows = await session.execute(
        select(UserDailyStats.tokens_used).where(
            UserDailyStats.user_id == user_id, UserDailyStats.activity_date == target_date
        )
    )
    return rows.scalars().first() or 0


def _transcript(messages: list[Message]) -> str:
    return "\n".join(
        f"{'캐피' if m.sender == 'moly' else '사용자'}: {m.content}" for m in messages
    )


async def _self_check(body: str, transcript: str) -> bool:
    """Haiku 환각 검사 — 'OK'면 통과. 오류/모호 시 통과(과잉 거부 방지)."""
    try:
        result = await llm.generate(
            self_check_prompt(),
            [{"role": "user", "content": f"[대화]\n{transcript}\n\n[일기]\n{body}"}],
            model=settings.anthropic_model_utility,
            max_tokens=8,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("self-check 오류(통과 처리): %r", e)
        return True
    return "NO" not in result.text.strip().upper()


async def _personal(profile, messages: list[Message]) -> tuple[str, str] | None:
    transcript = _transcript(messages)
    result = await llm.generate(
        diary_prompt(profile.language),
        [{"role": "user", "content": transcript}],
        model=settings.anthropic_model_diary,  # 대화 모델 A/B와 분리(일기 품질 고정)
    )
    weather, body = parse(result.text)
    if not body or not await _self_check(body, transcript):
        return None  # self-check 실패 → 호출측이 preset 폴백
    return body, weather


async def _pick_ment(session: AsyncSession) -> MolyLifeMent | None:
    rows = await session.execute(
        select(MolyLifeMent).where(MolyLifeMent.is_active.is_(True)).order_by(func.random()).limit(1)
    )
    return rows.scalars().first()


async def generate_for_user(
    session: AsyncSession, profile, target_date: date, cfg: dict[str, Any]
) -> None:
    """전일 일기 1건 생성(멱등). profile = Profile(또는 동형: id·timezone·language)."""
    if await _diary_exists(session, profile.id, target_date):
        return

    messages = await _day_messages(session, profile.id, target_date)
    # 개인일기 게이트 = 당일 유저 메시지 문자수(토큰 카운터와 분리 → 회계/캐싱 변경에 불변).
    user_chars = sum(len(m.content or "") for m in messages if m.sender == "user")

    source, weather, content, preset_id = "preset", "cloudy", None, None
    if messages and user_chars >= cfg["diary_min_user_chars"]:
        personal = await _personal(profile, messages)
        if personal is not None:
            content, weather = personal
            source = "llm"

    if source == "preset":
        ment = await _pick_ment(session)
        if ment is not None:
            content, weather, preset_id = ment.content, ment.weather, ment.id
        else:
            content = "오늘도 그냥저냥 하루가 갔다."  # 풀 비었을 때 안전 기본

    session.add(
        Diary(
            user_id=profile.id, diary_date=target_date, source=source,
            preset_ment_id=preset_id, content=content, weather=weather,
            published_at=publish_at(target_date, profile.timezone),
        )
    )
    await session.commit()

    # 기억 통합(mem0) — 실패해도 일기 생성은 유지(best-effort)
    if messages:
        try:
            await memory.add_conversation(
                str(profile.id),
                [
                    {"role": "assistant" if m.sender == "moly" else "user", "content": m.content}
                    for m in messages
                ],
            )
            # 새 기억 반영 → 채팅 기억 스냅샷 무효화(다음 대화가 당일 기억을 lazy 재로드)
            await session.execute(
                text("UPDATE chat_contexts SET memory_refreshed_at = NULL WHERE user_id = :u"),
                {"u": str(profile.id)},
            )
            await session.commit()
        except Exception as e:  # noqa: BLE001
            _log.warning("기억 통합 실패(user=%s): %r", profile.id, e)
