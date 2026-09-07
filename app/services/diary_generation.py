"""일기 생성 배치 로직 — 워커가 04:00 틱에 전일 일기를 만든다.

사용자 메시지 문자 수 충족 시 개인 일기, 미달이면 운영 원고를 사용한다.
주간 전환일 이후에는 사용자별 미수령 원고를 순서대로 지급한다.
"""
from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core.advisory_lock import advisory_xact_lock
from app.core.time_utils import safe_zone
from app.models.diary import Diary
from app.models.message import Message
from app.models.profile import Profile
from app.models.moly_life_ment import MolyLifeMent
from app.models.user_daily_stats import UserDailyStats
from app.services import diary_recall_repo, i18n, llm, naming, text_clean, usage_ledger
from app.services import config_store, diary_prompts, privacy
from app.services.diary_prompts import diary_prompt, parse, self_check_prompt

_log = logging.getLogger("moly-worker")


@dataclass(frozen=True)
class DiaryPolicy:
    weekly_start_date: date | None = None

    def is_weekly(self, target_date: date) -> bool:
        return self.weekly_start_date is not None and target_date >= self.weekly_start_date


async def load_policy(session: AsyncSession) -> DiaryPolicy:
    """미설정은 날짜별 방식. 잘못된 설정/조회 실패는 생성 실패로 처리한다."""
    raw = (await config_store.get_config_values(session, ["diary_weekly_start_date"])).get(
        "diary_weekly_start_date"
    )
    if raw is None:
        return DiaryPolicy()
    if not isinstance(raw, str):
        raise ValueError("diary_weekly_start_date must be an ISO Monday")
    try:
        start = date.fromisoformat(raw)
    except ValueError:
        raise ValueError("diary_weekly_start_date must be an ISO Monday") from None
    if start.isoformat() != raw or start.weekday() != 0:
        raise ValueError("diary_weekly_start_date must be an ISO Monday")
    return DiaryPolicy(start)


def _week_start(target_date: date) -> date:
    return target_date - timedelta(days=target_date.weekday())


def _ment_snapshot(ment):
    if ment is None:
        return None
    return SimpleNamespace(
        id=ment.id, content=ment.content, weather=ment.weather,
        week_start_date=getattr(ment, "week_start_date", None),
        sequence_no=getattr(ment, "sequence_no", None),
    )


async def _pick_weekly_ment(
    session: AsyncSession, user_id, week_start: date, *, lock: bool = False,
):
    stmt = text(
        "SELECT m.id,m.content,m.weather,m.week_start_date,m.sequence_no "
        "FROM moly_life_ments m "
        "WHERE m.week_start_date=:week AND m.is_active=true "
        "AND NOT EXISTS (SELECT 1 FROM diary_generation_results r "
        "WHERE r.user_id=:uid AND r.status='preset' AND r.preset_ment_id=m.id) "
        "ORDER BY m.sequence_no LIMIT 1" + (" FOR SHARE OF m" if lock else "")
    )
    row = (await session.execute(stmt, {"week": week_start, "uid": user_id})).mappings().first()
    return SimpleNamespace(**row) if row is not None else None


async def _lock_and_check(session: AsyncSession, user_id, target_date: date) -> bool:
    await advisory_xact_lock(session, user_id)
    await privacy.ensure_subject_active(session, user_id)
    if await session.scalar(select(Profile.id).where(Profile.id == user_id)) is None:
        raise RuntimeError("diary profile no longer exists")
    return await _diary_exists(session, user_id, target_date)


def publish_at(target_date: date, tz_name: str) -> datetime:
    """전일(target_date) 일기 발행 = 익일 로컬 09:00 → UTC."""
    local = datetime.combine(target_date + timedelta(days=1), time(9, 0), tzinfo=safe_zone(tz_name))
    return local.astimezone(timezone.utc)


async def _diary_exists(session: AsyncSession, user_id, target_date: date) -> bool:
    row = await session.execute(
        select(Diary.id).where(
            Diary.user_id == user_id,
            Diary.activity_date == target_date,
            Diary.kind.in_(("shared_day", "capi_day")),
            Diary.deleted_at.is_(None),
        )
    )
    if row.scalars().first() is not None:
        return True
    processed = await session.scalar(
        text(
            "SELECT 1 FROM diary_generation_results "
            "WHERE user_id=:user_id AND target_date=:target_date AND status IN ('no_entry','preset')"
        ),
        {"user_id": user_id, "target_date": target_date},
    )
    return processed is not None


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


_USER_LABEL = {"ko": "그 사람", "en": "that person", "ja": "その人"}


def _transcript(
    messages: list[Message], nickname: str | None = None, language: str | None = None
) -> str:
    """대화록. 유저 화자 라벨 = 닉네임(없으면 언어별 기본). '사용자'는 일기 본문으로 새어 나온다.

    저장 본문은 placeholder이므로 LLM 투입 전 현재 이름으로 렌더한다(유창성·추출 품질).
    """
    user_label = nickname or i18n.pick(_USER_LABEL, language)
    return "\n".join(
        f"{'캐피' if m.sender == 'moly' else user_label}: {naming.render(m.content, nickname)}"
        for m in messages
    )


async def _self_check(
    body: str, transcript: str, user_id=None, nickname: str | None = None,
    *, language: str | None = None, ledger: usage_ledger.LedgerContext | None = None,
) -> bool:
    """Haiku 환각 검사 — 첫 토큰이 'NO'면 탈락. 오류/모호 시 통과(과잉 거부 방지).

    판정은 앞부분으로만 한다. 'NO' 포함 여부로 보면 설명문에 섞인 'NO'에 오판한다.
    """
    try:
        # 검사 지시와 구획 라벨을 검사 대상과 같은 언어로 맞춘다. 영어 일기를 한국어 지시로
        # 검사하면 판정이 흔들린다.
        talk_label, diary_label = diary_prompts.self_check_labels(language)
        result = await llm.generate(
            self_check_prompt(language),
            [{
                "role": "user",
                "content": f"{talk_label}\n{transcript}\n\n{diary_label}\n{body}",
            }],
            model=settings.model_utility,
            max_tokens=16,
            ledger=usage_ledger.with_purpose(ledger, "diary_self_check"),
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("self-check 오류(통과 처리): %r", e)
        return True
    verdict = result.text.strip()
    passed = not verdict.upper().lstrip("*_# ").startswith("NO")
    if not passed:
        # 비차단 모니터링 — 발행은 하되 리젝률 추적용 로그(과거엔 preset 폴백 → 열람율 누수였음).
        # ⚠️ body와 verdict **둘 다** 실명이 들어 있을 수 있다. body는 LLM 생성 원문이고,
        # verdict는 실명이 렌더된 대화·일기를 입력으로 받은 모델의 응답이라 "NO: 승민이 언급은
        # 근거 없음"처럼 이름을 되뱉는다. 저장 경로는 나중에 to_placeholder를 타지만 로그는
        # 그 전이므로 여기서 둘 다 토큰으로 바꾼다.
        # ⚠️ **자르기 전에 마스킹한다.** 순서가 반대면 절단 경계가 이름 중간을 지날 때
        # ("승민" → "승") 마스킹이 그 조각을 못 찾아 평문으로 남는다.
        _log.warning(
            "self-check 리젝(비차단, 발행됨) user=%s 판정=%r 일기=%r",
            user_id,
            (naming.to_placeholder(verdict, nickname) or "")[:60],
            (naming.to_placeholder(body, nickname) or "")[:100],
        )
    return passed


# 개인일기 서지컬 복원 — 깨진문자(�)로 단어 잘림·한자/가나 섞임을 '그 부분만' 고친다.
# 결정적 삭제는 잘린 단어를 못 살리므로(메� → 메) LLM이 문맥으로 부분수정. 개인일기(LLM 생성)만
# 대상 — 프리셋은 시드 검증된 사람 글이라 strip_symbols로 충분. 배치라 지연 여유.
# 외래문자 판정·제거는 챗과 공용(text_clean.has_foreign / strip_foreign) — 단일 소스(SOMA-345).
# ⚠️ 이 복원은 '한국어 일기'에만 적용한다(호출측 _personal의 is_ko 게이팅). 비한국어(ja/zh)는
# CJK가 정상 본문이라 여기서 지우면 안 된다.
_MIN_EDIT_RATIO = 0.80  # 원문 대비 유사도 하한 — 이보다 크게 바뀌면 '부분수정' 아님 → 결정적 폴백
_SURGICAL_SYS = (
    "다음 한국어 일기에 깨진 문자(�)나 한자 또는 일본어 문자가 섞여 있다. "
    "문제가 된 그 글자만 문맥에 맞는 자연스러운 한국어로 고쳐라. "
    "나머지 표현 말투 내용 사실관계(이름 숫자 날짜)는 한 글자도 바꾸지 마라. "
    "깨진 문자로 단어가 잘린 경우 문맥상 가장 자연스러운 한국어로 최소한으로만 복원하고 "
    "확신이 없으면 억지로 지어내지 마라. 설명 없이 고친 일기만 출력해라."
)


def _needs_repair(body: str, *, nickname: str | None = None) -> bool:
    """깨진문자·외래문자가 있으면 서지컬 복원 대상(없으면 LLM 안 탐).

    닉네임은 판정에서 뺀다. 유저가 이름을 한글 밖 글자로 지어 두면(예: 키릴) 그 이름 때문에
    멀쩡한 일기가 매번 복원 대상으로 잡혀 LLM을 헛돈다.
    """
    return bool(body) and (
        "�" in body or text_clean.has_foreign(body, language="ko", keep=nickname)
    )


def _fallback_clean(body: str, *, keep_hyphen: bool = False, nickname: str | None = None) -> str:
    """복원 실패·과편집 시 결정적 폴백 — 외래문자·깨짐 제거(단어 깨질 수 있으나 마지막 안전망).

    닉네임은 지우지 않는다. 이름이 사라진 일기가 발행되는 쪽이 훨씬 나쁘다.
    """
    return text_clean.strip_symbols(
        text_clean.strip_foreign(body.replace("�", ""), language="ko", keep=nickname),
        keep_hyphen=keep_hyphen,
    )


async def _surgical_repair(
    body: str,
    *,
    user_id=None,
    nickname: str | None = None,
    ledger: usage_ledger.LedgerContext | None = None,
) -> str:
    """깨진 부분만 Haiku로 부분수정. 최소편집 가드(유사도)·재검사·재시도 후 안 되면 결정적 폴백."""
    for _ in range(2):
        try:
            r = await llm.generate(
                _SURGICAL_SYS, [{"role": "user", "content": body}],
                model=settings.model_utility, max_tokens=min(len(body) * 2 + 64, 512),
                ledger=usage_ledger.with_purpose(ledger, "diary_repair"),
            )
        except Exception as e:  # noqa: BLE001  # 복원 실패가 일기 발행을 막지 않게
            _log.warning("일기 서지컬 복원 호출 실패(폴백) user=%s: %r", user_id, e)
            return _fallback_clean(body, nickname=nickname)
        cand = r.text.strip()
        ratio = difflib.SequenceMatcher(None, body, cand).ratio()
        if not _needs_repair(cand, nickname=nickname) and ratio >= _MIN_EDIT_RATIO:
            _log.info("일기 서지컬 복원 user=%s ratio=%.2f", user_id, ratio)
            return cand
    _log.warning("일기 서지컬 복원 실패(과편집/미해결) 폴백 user=%s", user_id)
    return _fallback_clean(body, nickname=nickname)


async def _personal(
    profile, messages: list[Message],
    *, ledger: usage_ledger.LedgerContext | None = None,
) -> tuple[tuple[str, str] | None, dict[str, Any]]:
    """(본문, 날씨) 또는 None + 진단정보. None이면 호출측이 preset 폴백."""
    nickname = getattr(profile, "nickname", None)
    lang = getattr(profile, "language", None)
    is_ko = i18n.is_korean(lang)  # 미설정=영어. 비ko(en/ja)는 하이픈 유지 + 외래문자 복원 우회
    transcript = _transcript(messages, nickname, lang)
    result = await llm.generate(
        diary_prompt(lang, nickname),
        [{"role": "user", "content": transcript}],
        model=settings.model_diary,  # 대화 모델과 분리(일기 품질 고정) — provider는 prefix 라우팅
        ledger=usage_ledger.with_purpose(ledger, "diary_generate"),
    )
    weather, body = parse(result.text)
    # 외래문자(한자·가나) 서지컬 복원은 '한국어 일기'에만. 비한국어(ja/zh)는 CJK가 정상 본문이라
    # 지우면 안 됨(AC). 깨진문자(�)는 아래 strip_symbols(JUNK)가 언어 불문 제거한다.
    if is_ko and _needs_repair(body, nickname=nickname):
        body = await _surgical_repair(
            body, ledger=ledger, user_id=getattr(profile, "id", None), nickname=nickname
        )
    body = text_clean.strip_symbols(body, keep_hyphen=not is_ko)  # 마크다운·말줄임표 제거(비ko 하이픈 유지)
    # 언어를 가리지 않는 마지막 안전망. 한국어는 위 서지컬 복원이 먼저 맡지만, 그게 실패했거나
    # ja·en이라 복원을 안 탄 경우 여기서 결정적으로 지운다. 닉네임은 응답 언어와 계열이 달라도
    # 정상이므로 판정에서 뺀다.
    body = text_clean.strip_foreign(body, language=lang, keep=nickname)
    if not body:
        _log.warning("개인일기 본문 비어 폐기(preset 폴백) user=%s", getattr(profile, "id", None))
        return None, {"empty_body": True, "self_check_passed": None}
    # self-check는 비차단 — 게이트 통과 유저는 리젝돼도 개인일기 발행(preset 누수 차단). 로그만 남긴다.
    passed = await _self_check(
        body, transcript, user_id=getattr(profile, "id", None), nickname=nickname,
        language=lang, ledger=ledger
    )
    return (body, weather), {"empty_body": False, "self_check_passed": passed}


async def _pick_ment(session: AsyncSession, target_date: date) -> MolyLifeMent | None:
    """캐피 자기일기 소스 선택 — 그날 **지정본만**(SOMA-389). 매일 랜덤 폴백 폐지: 우리가 날짜
    지정으로 넣은 날에만 캐피 일기를 발행하고, 없으면 그날은 일기 없음(tombstone)."""
    dated = await session.execute(
        select(MolyLifeMent)
        .where(MolyLifeMent.is_active.is_(True), MolyLifeMent.diary_date == target_date)
        .limit(1)
    )
    return dated.scalars().first()


_TRANSLATE_SYS = (
    "You translate a short Korean first-person diary into natural {lang}. "
    "Keep the gentle, understated diary tone and the first-person voice. "
    "Output only the translated diary — nothing else, no notes, no Korean or other script."
)


async def _translate_preset(
    content: str, language: str, *, user_id=None,
    ledger: usage_ledger.LedgerContext | None = None,
) -> str:
    """preset(캐피 자기일기) 한국어 카피를 유저 언어로 번역. 실패 시 원문 유지(발행은 막지 않음)."""
    try:
        r = await llm.generate(
            _TRANSLATE_SYS.format(lang=i18n.resolve(language)),  # ko·en·ja 밖은 영어
            [{"role": "user", "content": content}],
            model=settings.model_utility,
            max_tokens=512,
            ledger=usage_ledger.with_purpose(ledger, "diary_translate"),
        )
    except Exception as e:  # noqa: BLE001  # 번역 실패가 일기 발행을 막지 않게
        _log.warning("preset 번역 실패(원문 유지) user=%s lang=%s: %r", user_id, language, e)
        return content
    return r.text.strip() or content


async def _finalize_diary(
    session: AsyncSession, profile, target_date: date, *, policy: DiaryPolicy,
    source: str, content: str | None, weather: str, ment, messages: list,
) -> dict[str, Any]:
    """사용자 잠금 안에서 재확인 후 일기/지급이력/회상 잡을 함께 확정한다."""
    if await _lock_and_check(session, profile.id, target_date):
        await session.rollback()
        return {"created": False, "skipped": True, "reason": "already_exists"}
    weekly = policy.is_weekly(target_date)
    if source == "preset":
        if weekly:
            current = await _pick_weekly_ment(
                session, profile.id, _week_start(target_date), lock=True,
            )
        else:
            current = _ment_snapshot(await _pick_ment(session, target_date))
        # 원고 선택 이후 다른 날짜에 지급됐거나 회수/수정되면 새 후보를 준비한다.
        if (
            (current is None) != (ment is None)
            or (current is not None and (
                current.id != ment.id or current.content != ment.content
                or current.weather != ment.weather
            ))
        ):
            await session.rollback()
            return {"retry": True, "reason": "candidate_changed"}
        if current is None:
            reason = "no_scheduled_entry"
            if weekly:
                active = await session.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM moly_life_ments "
                    "WHERE week_start_date=:week AND is_active=true)"
                ), {"week": _week_start(target_date)})
                reason = "weekly_exhausted" if active else "weekly_unavailable"
            await session.execute(text(
                "INSERT INTO diary_generation_results(user_id,target_date,status) "
                "VALUES (:uid,:day,'no_entry')"
            ), {"uid": profile.id, "day": target_date})
            await session.commit()
            return {"created": False, "skipped": False, "reason": reason, "source": "none"}
    if not content or not content.strip():
        raise ValueError("cannot publish an empty diary")
    kind = "shared_day" if source == "llm" else "capi_day"
    diary = Diary(
        user_id=profile.id, diary_date=target_date, kind=kind,
        activity_date=target_date, display_date=target_date, title=None, author="capi",
        occurred_at=datetime.combine(target_date, time(12), tzinfo=safe_zone(profile.timezone))
            .astimezone(timezone.utc),
        occurred_timezone=profile.timezone, occurred_timezone_provenance="profile_snapshot",
        primary_subject="user" if kind == "shared_day" else "capi",
        about_tags=["user"] if kind == "shared_day" else ["capi"],
        source=source, preset_ment_id=ment.id if source == "preset" else None,
        content=content, weather=weather, published_at=publish_at(target_date, profile.timezone),
    )
    session.add(diary)
    await session.flush()
    if weekly and source == "preset":
        await session.execute(text(
            "INSERT INTO diary_generation_results(user_id,target_date,status,preset_ment_id) "
            "VALUES (:uid,:day,'preset',:ment)"
        ), {"uid": profile.id, "day": target_date, "ment": ment.id})
    if kind == "shared_day":
        await diary_recall_repo.record_diary_sources(
            session, user_id=profile.id, diary_id=diary.id,
            message_ids=[m.id for m in messages if m.sender == "user"],
        )
    await diary_recall_repo.upsert_diary_recall_document(
        session, user_id=profile.id, diary_id=diary.id,
    )
    await session.commit()
    return {"created": True, "skipped": False, "source": source, "diary_id": str(diary.id)}


def _is_generation_conflict(exc: IntegrityError) -> bool:
    orig = getattr(exc.orig, "__cause__", None) or exc.orig
    return getattr(orig, "constraint_name", None) in {
        "diaries_one_daily_uq", "diary_generation_results_pkey",
        "diary_generation_results_user_preset_uq",
    }


async def generate_for_user(
    session: AsyncSession, profile, target_date: date, cfg: dict[str, Any], *, policy: DiaryPolicy,
) -> dict[str, Any]:
    """개인 우선, 운영 원고 대체, 미발행. 네트워크와 최종 DB 확정 구간을 분리한다."""
    profile = SimpleNamespace(**{
        key: getattr(profile, key, None) for key in ("id", "timezone", "language", "nickname")
    })
    try:
        return await _generate_for_user(session, profile, target_date, cfg, policy=policy)
    except BaseException:
        await session.rollback()
        raise


async def _generate_for_user(session, profile, target_date, cfg, *, policy):
    gate = cfg["diary_min_user_chars"]
    if await _diary_exists(session, profile.id, target_date):
        return {"created": False, "skipped": True, "reason": "already_exists"}
    await privacy.ensure_subject_active(session, profile.id)
    messages = [SimpleNamespace(id=m.id, sender=m.sender, content=m.content)
                for m in await _day_messages(session, profile.id, target_date)]
    user_chars = sum(len(m.content or "") for m in messages if m.sender == "user")
    gate_passed = bool(messages) and user_chars >= gate
    ledger = usage_ledger.LedgerContext(
        lane=usage_ledger.LANE_BACKGROUND, purpose="diary_generate",
        user_id=profile.id, activity_date=target_date,
    )
    await session.rollback()  # snapshot 완료. LLM 호출 중 DB 연결을 보유하지 않는다.
    source, content, weather = "preset", None, "cloudy"
    diag = {"empty_body": None, "self_check_passed": None}
    if gate_passed:
        personal, diag = await _personal(profile, messages, ledger=ledger)
        if personal is None:
            personal, diag = await _personal(profile, messages, ledger=ledger)
            diag = {**diag, "retried": True}
        if personal is not None:
            content, weather = personal
            content = naming.to_placeholder(content, profile.nickname)
            source = "llm"
    diagnostics = {
        "user_chars": user_chars, "gate": gate, "gate_passed": gate_passed,
        "personal_attempted": gate_passed, "empty_body": diag.get("empty_body"),
        "self_check_passed": diag.get("self_check_passed"),
    }
    for _ in range(3):
        ment = None
        if source == "preset":
            if policy.is_weekly(target_date):
                ment = await _pick_weekly_ment(session, profile.id, _week_start(target_date))
            else:
                ment = _ment_snapshot(await _pick_ment(session, target_date))
            await session.rollback()
            if ment is not None:
                original = text_clean.strip_symbols(ment.content)
                content, weather = original, ment.weather
                if not i18n.is_korean(profile.language):
                    content = await _translate_preset(
                        original, profile.language, user_id=profile.id, ledger=ledger,
                    )
                    content = text_clean.strip_symbols(content, keep_hyphen=True) or original
        try:
            result = await _finalize_diary(
                session, profile, target_date, policy=policy, source=source,
                content=content, weather=weather, ment=ment, messages=messages,
            )
        except IntegrityError as exc:
            await session.rollback()
            if not _is_generation_conflict(exc):
                raise
            continue
        if result.get("retry"):
            continue
        if result.get("skipped"):
            return result
        # no_entry 스키마는 개인 생성 세부 필드를 갖지 않는다.
        if result.get("source") == "none":
            return {**{k: v for k, v in diagnostics.items()
                       if k not in ("empty_body", "self_check_passed")}, **result}
        return {**diagnostics, **result}
    raise RuntimeError("diary generation contention; retry next tick")
