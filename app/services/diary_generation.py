"""일기 생성 배치 로직 — 워커가 04:00 틱에 전일 일기를 만든다.

분기(ERD §5.3): 전일 누적토큰 ≥ 임계 → 개인(llm, Sonnet 생성 + Haiku self-check)
              / 미달·미접속 → 캐피(preset, 멘트 풀). 멱등: unique(user, diary_date).
"""
from __future__ import annotations

import difflib
import logging
import re
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
from app.services import llm, memory, text_clean, naming
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
    """대화록. 유저 화자 라벨 = 닉네임(없으면 '그 사람'). '사용자'는 일기 본문으로 새어 나온다.

    저장 본문은 placeholder이므로 LLM 투입 전 현재 이름으로 렌더한다(유창성·추출 품질).
    """
    user_label = nickname or "그 사람"
    return "\n".join(
        f"{'캐피' if m.sender == 'moly' else user_label}: {naming.render(m.content, nickname)}"
        for m in messages
    )


async def _self_check(body: str, transcript: str, user_id=None) -> bool:
    """Haiku 환각 검사 — 첫 토큰이 'NO'면 탈락. 오류/모호 시 통과(과잉 거부 방지).

    판정은 앞부분으로만 한다. 'NO' 포함 여부로 보면 설명문에 섞인 'NO'에 오판한다.
    """
    try:
        result = await llm.generate(
            self_check_prompt(),
            [{"role": "user", "content": f"[대화]\n{transcript}\n\n[일기]\n{body}"}],
            model=settings.model_utility,
            max_tokens=16,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("self-check 오류(통과 처리): %r", e)
        return True
    verdict = result.text.strip()
    passed = not verdict.upper().lstrip("*_# ").startswith("NO")
    if not passed:
        # 비차단 모니터링 — 발행은 하되 리젝률 추적용 로그(과거엔 preset 폴백 → 열람율 누수였음).
        _log.warning(
            "self-check 리젝(비차단, 발행됨) user=%s 판정=%r 일기=%r",
            user_id, verdict[:40], body[:80],
        )
    return passed


# 개인일기 서지컬 복원 — 깨진문자(�)로 단어 잘림·한자/가나 섞임을 '그 부분만' 고친다.
# 결정적 삭제는 잘린 단어를 못 살리므로(메� → 메) LLM이 문맥으로 부분수정. 개인일기(LLM 생성)만
# 대상 — 프리셋은 시드 검증된 사람 글이라 strip_symbols로 충분. 배치라 지연 여유.
# NOTE: 한자/가나 정규식은 챗(text_clean.has_foreign_ko)과 중복 — #64·#65 병합 후 공용화(DRY) 예정.
_FOREIGN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]")
_MIN_EDIT_RATIO = 0.80  # 원문 대비 유사도 하한 — 이보다 크게 바뀌면 '부분수정' 아님 → 결정적 폴백
_SURGICAL_SYS = (
    "다음 한국어 일기에 깨진 문자(�)나 한자 또는 일본어 문자가 섞여 있다. "
    "문제가 된 그 글자만 문맥에 맞는 자연스러운 한국어로 고쳐라. "
    "나머지 표현 말투 내용 사실관계(이름 숫자 날짜)는 한 글자도 바꾸지 마라. "
    "깨진 문자로 단어가 잘린 경우 문맥상 가장 자연스러운 한국어로 최소한으로만 복원하고 "
    "확신이 없으면 억지로 지어내지 마라. 설명 없이 고친 일기만 출력해라."
)


def _needs_repair(body: str) -> bool:
    """깨진문자·한자/가나가 있으면 서지컬 복원 대상(없으면 LLM 안 탐)."""
    return bool(body) and ("�" in body or _FOREIGN.search(body) is not None)


def _fallback_clean(body: str) -> str:
    """복원 실패·과편집 시 결정적 폴백 — 외래문자·깨짐 제거(단어 깨질 수 있으나 마지막 안전망)."""
    return text_clean.strip_symbols(_FOREIGN.sub("", body.replace("�", "")))


async def _surgical_repair(body: str, *, user_id=None) -> str:
    """깨진 부분만 Haiku로 부분수정. 최소편집 가드(유사도)·재검사·재시도 후 안 되면 결정적 폴백."""
    for _ in range(2):
        try:
            r = await llm.generate(
                _SURGICAL_SYS, [{"role": "user", "content": body}],
                model=settings.model_utility, max_tokens=min(len(body) * 2 + 64, 512),
            )
        except Exception as e:  # noqa: BLE001  # 복원 실패가 일기 발행을 막지 않게
            _log.warning("일기 서지컬 복원 호출 실패(폴백) user=%s: %r", user_id, e)
            return _fallback_clean(body)
        cand = r.text.strip()
        ratio = difflib.SequenceMatcher(None, body, cand).ratio()
        if not _needs_repair(cand) and ratio >= _MIN_EDIT_RATIO:
            _log.info("일기 서지컬 복원 user=%s ratio=%.2f", user_id, ratio)
            return cand
    _log.warning("일기 서지컬 복원 실패(과편집/미해결) 폴백 user=%s", user_id)
    return _fallback_clean(body)


async def _personal(
    profile, messages: list[Message]
) -> tuple[tuple[str, str] | None, dict[str, Any]]:
    """(본문, 날씨) 또는 None + 진단정보. None이면 호출측이 preset 폴백."""
    nickname = getattr(profile, "nickname", None)
    transcript = _transcript(messages, nickname)
    result = await llm.generate(
        diary_prompt(profile.language, nickname),
        [{"role": "user", "content": transcript}],
        model=settings.model_diary,  # 대화 모델과 분리(일기 품질 고정) — provider는 prefix 라우팅
    )
    weather, body = parse(result.text)
    # 깨진문자(�)·한자/가나 → 서지컬 복원(그 부분만) 후 노이즈 정제. 없으면 LLM 안 탐.
    if _needs_repair(body):
        body = await _surgical_repair(body, user_id=getattr(profile, "id", None))
    body = text_clean.strip_symbols(body)  # 마크다운(**,-)·말줄임표 제거 (이름 마스킹 전이라 토큰 무영향)
    if not body:
        _log.warning("개인일기 본문 비어 폐기(preset 폴백) user=%s", getattr(profile, "id", None))
        return None, {"empty_body": True, "self_check_passed": None}
    # self-check는 비차단 — 게이트 통과 유저는 리젝돼도 개인일기 발행(preset 누수 차단). 로그만 남긴다.
    passed = await _self_check(body, transcript, user_id=getattr(profile, "id", None))
    return (body, weather), {"empty_body": False, "self_check_passed": passed}


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


_TRANSLATE_SYS = (
    "You translate a short Korean first-person diary into natural {lang}. "
    "Keep the gentle, understated diary tone and the first-person voice. "
    "Output only the translated diary — nothing else, no notes, no Korean or other script."
)


async def _translate_preset(content: str, language: str, *, user_id=None) -> str:
    """preset(캐피 자기일기) 한국어 카피를 유저 언어로 번역. 실패 시 원문 유지(발행은 막지 않음)."""
    try:
        r = await llm.generate(
            _TRANSLATE_SYS.format(lang=language),
            [{"role": "user", "content": content}],
            model=settings.model_utility,
            max_tokens=512,
        )
    except Exception as e:  # noqa: BLE001  # 번역 실패가 일기 발행을 막지 않게
        _log.warning("preset 번역 실패(원문 유지) user=%s lang=%s: %r", user_id, language, e)
        return content
    return r.text.strip() or content


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
        # personal is None = 빈 본문(드묾). self-check는 이제 비차단이라 리젝으론 None이 안 된다.
        # 빈 본문일 때만 1회 재생성(폐기율 제곱으로↓). 그래도 비면 preset.
        if personal is None:
            _log.info("개인일기 빈 본문 재생성 1회 시도(user=%s)", getattr(profile, "id", None))
            personal, retry_diag = await _personal(profile, messages)
            diag = {**retry_diag, "retried": True}
        if personal is not None:
            content, weather = personal
            # 개인일기 본문의 이름 → placeholder(egress에서 현재 이름 렌더). self-check 이후라 검사엔 무영향.
            content = naming.to_placeholder(content, getattr(profile, "nickname", None))
            source = "llm"

    if source == "preset":
        ment = await _pick_ment(session, target_date)
        if ment is not None:
            # 프리셋도 정제 통과(개인일기와 동일) — CSV/시드에 깨짐·부호 섞여도 저장 전 걸러낸다.
            content, weather, preset_id = text_clean.strip_symbols(ment.content), ment.weather, ment.id
            # 비한국어 유저는 preset(한국어 카피)을 유저 언어로 번역해 발행(우리가 넣는 일기도 언어 대응).
            plang = getattr(profile, "language", None)
            if plang and plang != "ko":
                content = await _translate_preset(content, plang, user_id=getattr(profile, "id", None))
                content = text_clean.strip_symbols(content)  # 번역이 부호를 재도입할 수 있어 재정제
        else:
            # 풀 비었을 때 안전 기본 — 언어별.
            _pl = getattr(profile, "language", None)
            content = "Another ordinary day went by." if _pl and _pl != "ko" else "오늘도 그냥저냥 하루가 갔다."

    diary = Diary(
        user_id=profile.id, diary_date=target_date, source=source,
        preset_ment_id=preset_id, content=content, weather=weather,
        published_at=publish_at(target_date, profile.timezone),
    )
    session.add(diary)
    await session.commit()

    # 기억 통합(mem0) — 실패해도 일기 생성은 유지(best-effort)
    mem_ok, mem_failed = 0, 0
    if messages:
        try:
            # M2: mem0 투입 전 현재 이름 렌더(추출 품질). mem0 custom_instructions가 이름을
            # 저장하지 않으므로 렌더된 텍스트를 줘도 장기기억에 이름은 안 남는다.
            nickname = getattr(profile, "nickname", None)
            await memory.add_conversation(
                str(profile.id),
                [
                    {
                        "role": "assistant" if m.sender == "moly" else "user",
                        "content": naming.render(m.content, nickname),
                    }
                    for m in messages
                ],
            )
            # 새 기억 반영 → 채팅 기억 스냅샷 무효화(다음 대화가 당일 기억을 lazy 재로드)
            await session.execute(
                text("UPDATE chat_contexts SET memory_refreshed_at = NULL WHERE user_id = :u"),
                {"u": str(profile.id)},
            )
            await session.commit()
            mem_ok = 1
        except Exception as e:  # noqa: BLE001
            _log.warning("기억 통합 실패(user=%s): %r", profile.id, e)
            mem_failed = 1

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
        "memory_ok": mem_ok,
        "memory_failed": mem_failed,
    }
