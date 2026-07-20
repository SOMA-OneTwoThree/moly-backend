"""일기 생성 배치 로직 — 워커가 04:00 틱에 전일 일기를 만든다.

분기(ERD §5.3): 전일 누적토큰 ≥ 임계 → 개인(llm, Sonnet 생성 + Haiku self-check)
              / 미달·미접속 → 캐피(preset, 멘트 풀). 멱등: unique(user, diary_date).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.diary import Diary
from app.models.message import Message
from app.models.moly_life_ment import MolyLifeMent
from app.models.user_daily_stats import UserDailyStats
from app.services import llm
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


def _transcript(messages: list[Message], nickname: str | None = None) -> str:
    """대화록. 유저 화자 라벨 = 닉네임(없으면 '그 사람'). '사용자'는 일기 본문으로 새어 나온다."""
    user_label = nickname or "그 사람"
    return "\n".join(
        f"{'캐피' if m.sender == 'moly' else user_label}: {m.content}" for m in messages
    )


async def _self_check(body: str, transcript: str, user_id=None) -> bool:
    """Haiku 환각 검사 — 첫 토큰이 'NO'면 탈락. 오류/모호 시 통과(과잉 거부 방지).

    판정은 앞부분으로만 한다. 'NO' 포함 여부로 보면 설명문에 섞인 'NO'에 오판한다.
    """
    try:
        result = await llm.generate(
            self_check_prompt(),
            [{"role": "user", "content": f"[대화]\n{transcript}\n\n[일기]\n{body}"}],
            model=settings.anthropic_model_utility,
            max_tokens=16,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("self-check 오류(통과 처리): %r", e)
        return True
    verdict = result.text.strip()
    passed = not verdict.upper().lstrip("*_# ").startswith("NO")
    if not passed:
        # 탈락 = 개인일기를 통째로 버리고 preset으로 폴백. 무음이면 재보정이 불가능하다.
        _log.warning(
            "self-check 탈락 → 개인일기 폐기(preset 폴백) user=%s 판정=%r 일기=%r",
            user_id, verdict[:40], body[:80],
        )
    return passed


async def _personal(
    profile, messages: list[Message]
) -> tuple[tuple[str, str] | None, dict[str, Any]]:
    """(본문, 날씨) 또는 None + 진단정보. None이면 호출측이 preset 폴백."""
    nickname = getattr(profile, "nickname", None)
    transcript = _transcript(messages, nickname)
    result = await llm.generate(
        diary_prompt(profile.language, nickname),
        [{"role": "user", "content": transcript}],
        model=settings.anthropic_model_diary,  # 대화 모델 A/B와 분리(일기 품질 고정)
    )
    weather, body = parse(result.text)
    if not body:
        _log.warning("개인일기 본문 비어 폐기(preset 폴백) user=%s", getattr(profile, "id", None))
        return None, {"empty_body": True, "self_check_passed": None}
    passed = await _self_check(body, transcript, user_id=getattr(profile, "id", None))
    if not passed:
        return None, {"empty_body": False, "self_check_passed": False}
    return (body, weather), {"empty_body": False, "self_check_passed": True}


async def _pick_ment(session: AsyncSession, target_date: date) -> MolyLifeMent | None:
    """캐피 자기일기 소스 선택 — 그날 지정본 우선, 없으면 날짜 없는 풀에서 랜덤."""
    dated = await session.execute(
        select(MolyLifeMent)
        .where(MolyLifeMent.is_active.is_(True), MolyLifeMent.diary_date == target_date)
        .limit(1)
    )
    ment = dated.scalars().first()
    if ment is not None:
        return ment
    # 폴백: 날짜 없는(diary_date IS NULL) 행만 랜덤 — 지정본이 다른 날 재사용되지 않게.
    rows = await session.execute(
        select(MolyLifeMent)
        .where(MolyLifeMent.is_active.is_(True), MolyLifeMent.diary_date.is_(None))
        .order_by(func.random())
        .limit(1)
    )
    return rows.scalars().first()


async def generate_for_user(
    session: AsyncSession, profile, target_date: date, cfg: dict[str, Any]
) -> dict[str, Any]:
    """전일 일기 1건 생성(멱등). profile = Profile(또는 동형: id·timezone·language).

    반환 = 진단정보(dev 엔드포인트·로깅용). 생성 자체의 성패는 예외로만 알린다.
    """
    gate = cfg["diary_min_user_chars"]
    if await _diary_exists(session, profile.id, target_date):
        return {"created": False, "skipped": True, "reason": "already_exists"}

    messages = await _day_messages(session, profile.id, target_date)
    # 개인일기 게이트 = 당일 유저 메시지 문자수(토큰 카운터와 분리 → 회계/캐싱 변경에 불변).
    user_chars = sum(len(m.content or "") for m in messages if m.sender == "user")

    source, weather, content, preset_id = "preset", "cloudy", None, None
    diag: dict[str, Any] = {"empty_body": None, "self_check_passed": None}
    gate_passed = bool(messages) and user_chars >= gate
    if gate_passed:
        personal, diag = await _personal(profile, messages)
        # self-check 탈락률이 낮지 않다(실측 ~40%). 한 번 더 뽑으면 폐기율이 제곱으로 준다.
        # 개인일기가 preset으로 새는 건 핵심 훅(일기 열람율) 직격이라 재생성 1회가 남는 장사다.
        if personal is None:
            _log.info("개인일기 재생성 1회 시도(user=%s)", getattr(profile, "id", None))
            personal, retry_diag = await _personal(profile, messages)
            diag = {**retry_diag, "retried": True}
        if personal is not None:
            content, weather = personal
            source = "llm"

    if source == "preset":
        ment = await _pick_ment(session, target_date)
        if ment is not None:
            content, weather, preset_id = ment.content, ment.weather, ment.id
        else:
            content = "오늘도 그냥저냥 하루가 갔다."  # 풀 비었을 때 안전 기본

    diary = Diary(
        user_id=profile.id, diary_date=target_date, source=source,
        preset_ment_id=preset_id, content=content, weather=weather,
        published_at=publish_at(target_date, profile.timezone),
    )
    session.add(diary)
    await session.commit()

    return {
        "created": True,
        "skipped": False,
        "source": source,  # llm = 개인일기 / preset = 캐피 자기일기
        "user_chars": user_chars,
        "gate": gate,
        "gate_passed": gate_passed,
        "personal_attempted": gate_passed,
        "empty_body": diag.get("empty_body"),
        "self_check_passed": diag.get("self_check_passed"),
        "diary_id": str(diary.id) if diary.id else None,
    }
