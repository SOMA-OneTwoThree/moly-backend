"""chat 서비스 — 상태·이력·전송·선발화. 대화는 HTTP 완성본(스트리밍 없음)."""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from math import ceil
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.core.advisory_lock import advisory_xact_lock
from app.core.time_utils import safe_zone
from app.models.chat_context import ChatContext
from app.models.greeting import Greeting
from app.models.idempotency_key import IdempotencyKey
from app.models.message import Message
from app.models.user_daily_stats import UserDailyStats
from app.schemas.chat import PostMessageResponse
from app.services import (
    checkpoint,
    checkpoint_repo,
    gating,
    greetings,
    i18n,
    llm,
    memory_repo,
    memory_forget,
    naming,
    prompts,
    privacy,
    relationship_profile,
    relationship_profile_repo,
    text_clean,
    turn_context,
    chat_references,
    chat_turns,
    diary as diary_service,
    episodic_memory,
)
from app.services.account import _uid
from app.services.agent import config as agent_config
from app.services.agent import runtime as agent_runtime
from app.services.prompts import system_prompt

_GREETING_CONTEXTS = greetings.CONTEXTS
_log = logging.getLogger("moly-backend")


def validate_post_message_response(
    payload: Any, *, user_id: str | None = None, idempotency_key: str | None = None
) -> PostMessageResponse:
    """현재 채팅 응답 계약을 저장·재사용 양쪽에서 검증한다.

    비호환 캐시를 새 요청으로 재실행하면 메시지와 토큰이 중복될 수 있으므로 반드시
    fail-closed 하고 행도 보존한다 — 삭제는 요청 경로가 아니라 운영 절차
    (scripts/verify_idempotency_responses.py --delete-invalid)에서만 한다(api-inventory.md).
    응답 본문은 민감할 수 있어 로그에 남기지 않는다. 반환한 모델 인스턴스를 라우트까지
    그대로 넘기면 response_model이 재검증하지 않아 요청당 검증이 1회로 끝난다.
    """
    try:
        return PostMessageResponse.model_validate(payload)
    except ValidationError as exc:
        _log.error(
            "채팅 멱등 응답 스키마 불일치(user=%s key=%s) — "
            "scripts/verify_idempotency_responses.py --delete-invalid로 정리 필요",
            user_id,
            idempotency_key,
        )
        raise errors.AppError(
            "INTERNAL",
            500,
            "일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
        ) from exc


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# --- GET /chat/state ---
async def get_state(session: AsyncSession, user_id: str) -> dict[str, Any]:
    await privacy.ensure_subject_active(session, _uid(user_id))
    g = await gating.resolve(session, user_id)
    ent = g.entitlement
    remaining = ent["tokens_remaining"]
    threshold = ent["personal_diary_token_threshold"]
    return {
        "activity_date": g.activity_date.isoformat(),
        "plan": ent["plan"],
        "tokens_used": g.tokens_used,
        "daily_token_limit": ent["daily_token_limit"],
        "tokens_remaining": remaining,
        "warning_threshold": g.warning_threshold,
        "personal_diary_eligible": threshold is not None and g.tokens_used >= threshold,
        "limit_reached": remaining == 0,
    }


# --- GET /chat/messages ---
def _msg_dto(m: Message, nickname: str | None) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "sender": m.sender,
        "content": naming.render(m.content, nickname),  # placeholder → 현재 이름
        "created_at": _iso(m.created_at),
    }


def _cursor_id(cursor: str) -> int:
    """숫자 커서 파싱 — 잘못된 값은 422(미가드 시 int() ValueError → 500)."""
    try:
        return int(cursor)
    except ValueError as e:
        raise errors.validation("잘못된 커서 형식이에요.") from e


async def get_messages(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int = 30,
    cursor: str | None = None,
    direction: str = "older",
    anchor_date: date | None = None,
    capabilities: str | None = None,
) -> dict[str, Any]:
    uid = _uid(user_id)
    await privacy.ensure_subject_active(session, uid)
    limit = max(1, min(limit, 100))
    from app.models.profile import Profile

    profile = await session.get(Profile, uid)
    nickname = profile.nickname if profile is not None else None
    base = select(Message).where(Message.user_id == uid)

    if anchor_date is not None:
        # 그 activity_date부터 최신 방향(오래된→최신)
        q = base.where(Message.activity_date >= anchor_date).order_by(Message.id.asc()).limit(limit)
        rows = list((await session.execute(q)).scalars().all())
    elif direction == "newer" and cursor:
        q = base.where(Message.id > _cursor_id(cursor)).order_by(Message.id.asc()).limit(limit)
        rows = list((await session.execute(q)).scalars().all())
    else:  # older (기본): 최신부터 과거로, 반환은 오래된→최신
        q = base
        if cursor:
            q = q.where(Message.id < _cursor_id(cursor))
        q = q.order_by(Message.id.desc()).limit(limit)
        rows = list(reversed((await session.execute(q)).scalars().all()))

    refs = await chat_references.hydrate_for_messages(
        session,
        user_id=uid,
        message_ids=[m.id for m in rows if m.sender == "moly"],
        nickname=nickname,
        capability_enabled="diary-reference-v1" in (capabilities or ""),
    )
    data = []
    for m in rows:
        item = _msg_dto(m, nickname)
        item["references"] = refs.get(m.id, [])
        data.append(item)
    return {
        "data": data,
        "older_cursor": str(rows[0].id) if rows else None,
        "newer_cursor": str(rows[-1].id) if rows else None,
    }


# --- 프롬프트용 컨텍스트(앵커 append-only) ---
_WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


def _date_label(d: date) -> str:
    return f"[{d.month}월 {d.day}일 {_WEEKDAYS[d.weekday()]}요일]"


def _mark_dates(convo: list[dict[str, str]], msgs: list[Message]) -> None:
    """날짜 그룹 첫 메시지에 절대 날짜 표식을 붙인다 — 캐피가 날짜 경계·경과를 인지하도록.

    절대 날짜(상대 '어제/오늘' 아님)라 옛 메시지의 표식이 날이 바뀌어도 안 변한다 → 캐시 프리픽스
    안정. 가장 최근 표식이 곧 오늘(이번 턴 유저 메시지가 항상 배열 끝). 페르소나가 그렇게 읽는다.
    """
    prev: date | None = None
    for slot, m in zip(convo, msgs):
        if m.activity_date != prev:
            slot["content"] = f"{_date_label(m.activity_date)}\n{slot['content']}"
            prev = m.activity_date


def _keep_window(rows: list[Message]) -> list[Message]:
    """리셋 시 유지할 최근 창 — KEEP 개수/문자 상한, user 메시지로 시작하게. KEEP ≪ RESET(헤드룸)."""
    kept: list[Message] = []
    chars = 0
    for m in reversed(rows):
        if len(kept) >= settings.context_keep_messages or chars >= settings.context_keep_chars:
            break
        kept.append(m)
        chars += len(m.content or "")
    kept.reverse()
    while kept and kept[0].sender == "moly":  # 첫 메시지 user 보장(Anthropic)
        kept.pop(0)
    return kept or rows[-1:]  # 최소 1개(최신 = 방금 flush된 user 메시지)


async def _context(
    session: AsyncSession,
    uid: uuid.UUID,
    anchor: int,
    *,
    current_text: str | None = None,
    current_date: date | None = None,
) -> tuple[list[dict[str, str]], int | None, list[Message]]:
    """앵커 이후 메시지로 대화 컨텍스트 조립. 세그먼트가 트리거 넘으면 새 앵커 반환(리셋).

    프리픽스는 리셋 때만 1회 바뀌고 그 사이엔 append-only → 캐시 히트 유지.

    셋째 반환값 = 대화 배열 맨 앞에서 밀려난 캐피 메시지(=커밋된 선발화).
    Anthropic이 messages[0]를 user로 강제해서 배열엔 못 넣지만, 버리면 캐피가 방금 건넨
    인사를 모른 채 또 인사한다. 호출측이 system 가변 블록으로 넘긴다.

    current_text가 주어지면(SOMA-374 read-only phase) 이번 턴 유저 메시지가 아직 DB에
    없으므로, 과거 메시지로 조립한 뒤 현재 턴을 배열 끝에 in-memory로 붙인다(날짜 표식 포함).
    리셋 카운트에도 현재 턴을 포함한다. current_text 없으면(단위 테스트) 기존 동작 그대로.

    현재 상태·focus 같은 서버 사실은 이 함수에 전달하지 않는다. 그 정보는
    ``_build_system``의 서버 소유 system 블록으로만 넣어 저장 데이터가 user 발화 권위를
    얻지 못하게 한다.
    """
    q = (
        select(Message)
        .where(Message.user_id == uid, Message.id >= anchor)
        .order_by(Message.id.desc())
        .limit(settings.context_hard_msg_cap)  # 안전 상한(정상 시 리셋 트리거가 먼저 걸림)
    )
    rows = list(reversed((await session.execute(q)).scalars().all()))

    extra_msgs = 1 if current_text is not None else 0
    extra_chars = len(current_text or "")
    new_anchor: int | None = None
    over_msgs = len(rows) + extra_msgs >= settings.context_reset_messages
    over_chars = (
        sum(len(m.content or "") for m in rows) + extra_chars >= settings.context_reset_chars
    )
    if over_msgs or over_chars:
        rows = _keep_window(rows)
        new_anchor = rows[0].id  # 앵커 전진(1회 프리픽스 변경)

    convo = [
        {"role": "assistant" if m.sender == "moly" else "user", "content": m.content}
        for m in rows
    ]
    lead: list[Message] = []
    while convo and convo[0]["role"] != "user":  # Anthropic: 첫 메시지 user 보장
        lead.append(rows[len(lead)])  # 버리지 않고 회수 — system으로 넘긴다
        convo.pop(0)
    kept = rows[len(lead):]  # convo와 정렬(같은 길이·순서) — 날짜 표식용
    if not convo and current_text is None:  # 빈 배열=400. 폴백은 현재 턴이 없을 때만(그땐 append가 채움)
        lead = []
        for m in reversed(rows):
            if m.sender != "moly":
                convo, kept = [{"role": "user", "content": m.content}], [m]
                break
    _mark_dates(convo, kept)
    if current_text is not None:  # 현재 턴을 배열 끝에 붙임 — 직전 kept와 날짜가 다르면 표식 부착
        content = current_text
        prev_date = kept[-1].activity_date if kept else None
        if current_date is not None and current_date != prev_date:
            content = f"{_date_label(current_date)}\n{current_text}"
        convo.append({"role": "user", "content": content})
    return convo, new_anchor, lead


async def _save_anchor(session: AsyncSession, uid: uuid.UUID, anchor: int) -> None:
    stmt = pg_insert(ChatContext).values(user_id=uid, anchor_message_id=anchor)
    # GREATEST: 앵커는 단조 전진만(역행 시 요약·세그먼트 중복 방지). 컬럼만 갱신(전체행 upsert 금지).
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id"],
        set_={
            "anchor_message_id": func.greatest(ChatContext.anchor_message_id, anchor),
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


async def _touch_last_active(session: AsyncSession, uid: uuid.UUID, now: datetime) -> None:
    """이번 턴 확정 시각을 저장한다. Phase 1은 이 쓰기 전 값을 읽으므로 직전 방문을 본다."""
    stmt = pg_insert(ChatContext).values(user_id=uid, last_active_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id"],
        set_={"last_active_at": now, "updated_at": func.now()},
    )
    await session.execute(stmt)


def _build_system(
    language: str,
    nickname: str | None,
    lead: list[str] | None = None,
    *,
    summary: str = "",
    relationship_text: str = "",
    current_state: str = "",
) -> list[str]:
    """system을 [페르소나(불변), 닉네임+선발화+기억+지난 이야기(가변)] 블록으로.
    뒤 블록이 바뀌어도 페르소나 캐시 생존.

    lead = 대화 배열에 못 넣은 선발화 내용(placeholder 저장 문자열 리스트, _context 참조).
    앵커가 전진하기 전까지 매 턴 같은 값이라 가변 블록도 그대로 유지된다 — 캐시가 추가로 깨지지 않는다.

    summary = 최신 대화 요약 checkpoint(W11). 여기(안정 프리픽스)에 두는 이유는 이 값이 **checkpoint
    행이 생길 때만** 바뀌기 때문이다. 매 턴 바뀌는 자리(마지막 user 메시지 등)에 넣으면 그 자리부터
    캐시가 매 턴 깨진다.

    다만 리셋 사이클당 프리픽스는 **두 번** 바뀐다 — 리셋 턴에 한 번, 요약 잡이 비동기라 도착한
    턴에 또 한 번. 동기 주입은 5초 제약상 불가하므로 이건 설계상 감수하는 비용이다(리셋 주기가
    40턴이라 턴당으로는 무시할 수준).
    """
    parts: list[str] = []
    if nickname:
        # 조사는 받침에 맞춰(승민이야 / 지호야) — 지시문이 틀리면 캐피도 따라 틀린다.
        parts.append(f"[상대]\n지금 얘기하는 사람 이름은 {greetings.copula(nickname)}.")
    if lead:
        # placeholder 저장분 → LLM 투입 전 현재 이름 렌더(유창성 유지).
        said = "\n".join(naming.render(t, nickname) for t in lead if t)
        parts.append(
            "[먼저 건넨 말]\n"
            "이 대화 직전에 네가 먼저 말을 걸었어. 상대는 그걸 보고 답한 거야. "
            "같은 인사를 또 하지 마.\n"
            f"{said}"
        )
    if relationship_text:
        block = prompts.relationship_profile_block(
            naming.render(relationship_text, nickname), language, enabled=True
        )
        if block:
            parts.append(block)
    if summary:
        # 대화 배열에는 앵커 이후만 남는다 — 그 앞 이야기는 이 요약이 대신한다(placeholder 저장분이라
        # 투입 전 현재 이름 렌더). 기억([기억])은 오래 남는 사실, 여기는 최근 대화의 줄거리다.
        parts.append(
            "[지난 이야기]\n"
            "아래 대화 앞에 오간 내용을 네가 정리해 둔 거야. 이미 아는 것처럼 자연스럽게 이어 말해.\n"
            f"{naming.render(summary, nickname)}"
        )
    if current_state:
        parts.append(
            "[지금 상태 - 서버 사실]\n아래 상태는 상대가 쓴 말이 아니라 지금 네 주변의 사실이야. "
            "항목을 읊지 말고 대화에 필요한 부분만 자연스럽게 녹여.\n"
            f"{current_state}"
        )
    dyn = "\n\n".join(parts)
    return [system_prompt(language), dyn] if dyn else [system_prompt(language)]


# 부호 정제 정규식은 text_clean으로 이관(chat·일기 공용). 여기선 되묻기 물음표 백스톱만.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
# 되묻기 물음표 백스톱 — 의문사가 문장 끝 8자 이내 + 의문 가능 어미일 때만 교정(위양성 0 실측).
_WH = re.compile(r"무슨|왜|어디|언제|누구|얼마|어때|어땠|어떻|어떤|뭔데|뭐야")
# 의문 가능 어미. '데'는 통째로(그런데/인데/는데 다 포함) — WH 근접 조건이 평서문 오삽입을 막는다.
_Q_END = re.compile(r"(데|야|어|지|까|래)$")


def _fix_qmarks(text: str, nickname: str | None) -> str:
    """부드러운 되묻기('무슨 일인데.', '무슨 일이야, 승민아.')에 빠진 물음표를 결정적으로 복원.

    모델은 이런 소프트 어미를 반쯤 평서문으로 처리해 마침표를 찍는다(실측 누락 ~17%).
    프롬프트만으론 천장이라 코드로 확정한다. 의문사가 끝 8자 이내 + 의문 어미일 때만 교체해
    평서문 오삽입을 막고('무슨 일이 있어도 괜찮아'는 의문사가 멀어 미교정), 끝의 호명은
    벗겨 검사하며('무슨 일이야, 승민아'), '~지 뭐'의 종결 particle은 제외한다.
    """
    voc = greetings.vocative(nickname) if nickname else None
    out: list[str] = []
    for s in _SENT_SPLIT.split(text):
        if not s or s.endswith(("?", "!")):
            out.append(s)
            continue
        core = s[:-1] if s.endswith(".") else s
        check = core
        if voc and core.endswith(voc):  # 끝의 호명(', 승민아')을 벗겨 어미 노출
            stripped = core[: -len(voc)].rstrip(" ,")
            if stripped:
                check = stripped
        if check.endswith("뭐"):  # '~지 뭐' — 여기 '뭐'는 의문사가 아니다
            out.append(s)
            continue
        m = list(_WH.finditer(check))
        near = bool(m) and len(check) - m[-1].end() <= 8
        # 선택의문문('A야 아니면 B야.') — A절이 의문 어미로 끝나고 '아니면'이 이어질 때만.
        # '아니면'만 보면 명령·제안 평서문("아니면 그냥 쉬어.")에 오삽입돼서 A절 어미를 요구한다.
        choice = bool(re.search(r"[야래까어지]\s*아니면", check))
        out.append(core + "?" if ((near or choice) and _Q_END.search(check)) else s)
    return " ".join(out)


def _clean_reply(text: str, nickname: str | None = None, language: str | None = None) -> str:
    """캐피 대사 정제 — 줄바꿈·말줄임표 제거 + 되묻기 물음표 복원.

    페르소나로 막아도 새서(실측) 코드로 확정한다. 채팅 말풍선은 한 덩어리 한 줄이고,
    말끝 흐리기는 캐피 톤이 아니다. 허용 부호(마침표·물음표·느낌표)만 남기고
    말줄임표·마크다운 강조(**,_)·대시(—)는 지운다. 쉼표는 코드로 지우지 않고 프롬프트에
    맡긴다(검증상 강제 제거는 런온을 만들어 짧은 문장 목표를 못 이룸).
    """
    keep_hy = not i18n.is_korean(language)  # en 등: 하이픈 유지 + 한국어 되묻기 물음표 복원 불필요
    out = text_clean.strip_symbols(text, keep_hyphen=keep_hy)  # 말줄임표·마크다운·대시 제거 + 공백 정규화
    return out if keep_hy else _fix_qmarks(out, nickname)


# 한국어 응답에 드물게 섞이는 한자·가나(LLM 디코딩 아티팩트) 복원 지시. 프롬프트로 빈도는 낮췄지만
# 0은 아니라(확률적 토큰 슬립) 코드 백스톱으로 확정한다. 삭제는 단어를 깨므로 재작성으로 복원.
_FOREIGN_REPAIR_SYS = (
    "다음 한국어 문장에 중국어 한자나 일본어 문자가 섞여 있다. "
    "그 글자만 문맥에 맞는 자연스러운 한국어로 바꿔라. "
    "나머지 표현 말투 문장부호는 절대 바꾸지 말고 그대로 둬라. "
    "설명 없이 고친 문장만 출력해라."
)


async def _repair_foreign_ko(
    reply: str, *, user_id: str | None = None
) -> tuple[str, list[llm.LlmCall]]:
    """한국어 응답에 섞인 한자·가나를 utility 모델로 재작성 복원. 호출측에서 language=='ko' 게이팅.

    최대 2회 시도 후에도 남으면 최후수단으로 제거(단어 깨질 수 있어 최후). 호출 실패는
    원문 유지(응답을 막지 않음). 실발동은 드문 이벤트라 지연·비용 영향은 무시 수준.

    반환 = (복원문, 이 함수가 실제로 소비한 LLM 호출 목록). 호출자가 턴 합계에 넣어 청구한다 —
    예전엔 이 호출들이 청구에서 통째로 누락됐다(실비용 ↔ 한도 불변식 깨짐).
    실패로 원문을 되돌리는 경우에도 그 전 시도는 이미 과금됐으므로 calls는 버리지 않는다.
    """
    text = reply
    calls: list[llm.LlmCall] = []
    for _ in range(2):
        try:
            r = await llm.generate(
                _FOREIGN_REPAIR_SYS,
                [{"role": "user", "content": text}],
                model=settings.model_utility,
                max_tokens=min(len(text) * 2 + 64, 512),  # 한 문장 교정분만(러너웨이 생성 방지)
                timeout=settings.llm_timeout_s,
            )
        except Exception as e:  # noqa: BLE001  # 복원 실패가 응답을 막지 않게
            _log.warning("한자 복원 호출 실패(원문 유지) user=%s: %r", user_id, e)
            return reply, calls
        calls.append(_llm_call(r, "foreign_repair"))
        text = r.text.strip()
        if not text_clean.has_foreign_ko(text):
            _log.info(  # 관측용 — 드문 이벤트라 발동 사실·토큰만 남긴다(청구엔 포함됨)
                "한자 복원 완료 user=%s in=%d out=%d", user_id, r.input_tokens, r.output_tokens
            )
            return text, calls
    _log.warning("한자 복원 2회 후에도 잔존 — 최후수단 제거 user=%s", user_id)
    return text_clean.strip_foreign_ko(text), calls


def _billable(r: llm.LLMResult) -> int:
    """실비용 가중 청구 토큰 = billable × 입력단가 = 실제 청구액(정확). 한도가 달러예산에 직결.

    provider마다 단가비율이 달라 가중치를 model prefix로 선택한다(OpenAI out 6.0·read 0.1·write 1.25 /
    Anthropic out 5.0·read 0.1·write 1.25). write는 cold 턴이 실제 더 비싸니 그만큼 더 셈.
    """
    if llm.provider_for(r.model) == "openai":
        w_out = settings.bill_weight_output_openai
        w_read = settings.bill_weight_cache_read_openai
        w_write = settings.bill_weight_cache_write_openai
    else:
        w_out = settings.bill_weight_output
        w_read = settings.bill_weight_cache_read
        w_write = settings.bill_weight_cache_write
    raw = (
        r.input_tokens
        + w_out * r.output_tokens
        + w_read * r.cache_read_tokens
        + w_write * r.cache_write_tokens
    )
    return ceil(raw)


def _llm_call(r: llm.LLMResult, purpose: str) -> llm.LlmCall:
    """LLMResult → 회계 단위 LlmCall. billable은 **호출별로** 계산한다(모델·provider가 섞여도 정확)."""
    return llm.LlmCall(
        provider=llm.provider_for(r.model),
        model=r.model,
        purpose=purpose,
        input_tokens=r.input_tokens,
        output_tokens=r.output_tokens,
        cache_read_tokens=r.cache_read_tokens,
        cache_write_tokens=r.cache_write_tokens,
        billable=_billable(r),
    )


@dataclass
class TurnUsage:
    """한 턴의 LLM 호출 전부를 모아 합산 — 차감·저장의 단일 기준.

    한 턴은 LLM을 여러 번 부른다(주 chat + 한자 복원, 이후 도구 루프). 예전엔 주 호출만 세서
    복원분이 청구에서 샜다. messages 행에는 여기 합계를 저장한다(컬럼·스키마 변경 없음).
    """
    calls: list[llm.LlmCall] = field(default_factory=list)

    @property
    def total_billable(self) -> int:
        return sum(c.billable for c in self.calls)

    @property
    def totals(self) -> dict[str, int]:
        """messages 컬럼과 같은 키로 4종 토큰 합계."""
        return {
            k: sum(getattr(c, k) for c in self.calls)
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        }


# --- W2: 턴 계측(§0.1 응답시간·턴수 제약 판정 근거) ---
def _emit_turn_metrics(**fields: Any) -> None:
    """턴 계측을 구조화 로그 1줄로 배출.

    유저 id·메시지 본문은 절대 넣지 않는다(길이·hash까지만 허용하되 여기선 아예 안 쓴다).
    배출이 실패해도(직렬화 불가·핸들러 오류 등) 유저 응답을 막으면 안 되므로 예외를 삼킨다 —
    계측 실패가 응답 실패로 번지는 게 최악이다.
    """
    try:
        _log.info("chat_turn_metrics %s", json.dumps(fields, default=str, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — 계측 배출 실패는 절대 응답을 막지 않는다
        _log.warning("chat_turn_metrics 로그 배출 실패", exc_info=True)


def _ms(t0: float, t1: float) -> float:
    return (t1 - t0) * 1000


# --- 유저 단위 직렬화(토큰 한도 TOCTOU 방지) ---
async def _lock_user(session: AsyncSession, uid: uuid.UUID) -> None:
    """트랜잭션 범위 advisory lock — 같은 유저의 동시 요청을 직렬화. 커밋/롤백 시 자동 해제.
    게이팅 전에 잠가야 동시요청이 같은 pre-burst tokens_used를 읽고 한도를 우회하는 걸 막는다.
    보상(economy·ads)과 **같은 직렬화 도메인**을 쓰도록 키 표현은 core.advisory_lock 단일 구현."""
    await advisory_xact_lock(session, uid)


# --- 정규화 기억 소스(W8, Phase 2 트랜잭션 안) ---
async def _record_memory_source(
    session: AsyncSession,
    uid: uuid.UUID,
    *,
    representative_message_id: int,
    message_ids: list[int],
    now: datetime,
) -> int | None:
    """이 턴의 watermark를 배정하고 정규화 기억 추출 잡을 건다.

    호출 위치가 계약이다: 유저·캐피 메시지를 insert한 **같은 Phase 2 트랜잭션·같은 유저락 안**,
    커밋 전. 그래야 `memory_source_turns`/`memory_source_turn_messages`/`async_jobs` 행이 메시지와
    원자적으로 생기고, watermark가 유저별로 빈틈 없이 하나씩 올라간다(§W8).

    실패 정책:
    - 대표가 inbound user message가 아니면(`NoInboundUserMessageError`) watermark를 쓰지 않고
      조용히 건너뛴다. 선발화만 있는 턴은 v1 추출 소스가 아니다 — 대화는 정상 응답한다.
    - 그 밖의 저장소 불변식 위반(`MemoryRepoError`)도 **쓰기 전에** 나므로 같은 방식으로 넘긴다
      (기억 배선 버그가 대화를 죽이지 않게).
    - DB 오류는 잡지 않고 올린다. 같은 트랜잭션이라 이미 abort된 상태이므로 fail-open이 성립하지
      않는다 — 삼키면 watermark만 오르고 추출 잡이 없는 턴이 조용히 생기거나, 뒤이은 커밋이
      알 수 없는 곳에서 실패한다. 올리면 이번 턴 전체가 롤백되고(저장 0) 클라가 같은 멱등키로
      깨끗이 재시도한다.
    """
    try:
        turn = await memory_repo.allocate_source_turn(
            session,
            user_id=uid,
            representative_message_id=representative_message_id,
            message_ids=message_ids,
            committed_at=now,
        )
    except memory_repo.MemoryRepoError as e:  # 쓰기 이전 단계에서만 발생 — 트랜잭션은 멀쩡하다
        _log.info("기억 소스 배정 건너뜀(user=%s): %s", uid, e)
        return None
    await memory_repo.enqueue_extraction(
        session,
        user_id=uid,
        memory_generation=turn.memory_generation,
        from_watermark=turn.watermark,
        through_watermark=turn.watermark,
        message_ids=turn.message_ids,
    )
    return turn.watermark


# --- 대화 요약 checkpoint(W11, Phase 2 트랜잭션 안) ---
async def _enqueue_checkpoint(
    session: AsyncSession, uid: uuid.UUID, *, anchor: int, keep_from: int
) -> None:
    """앵커 리셋으로 **버려질 구간**을 요약 잡으로 남긴다 — 킬스위치 off면 조회조차 하지 않는다.

    호출 위치가 계약이다: 이 턴의 메시지를 insert한 **같은 Phase 2 트랜잭션·유저락 안**, 커밋 전.
    그래야 요약 잡이 그 메시지들과 원자적으로 생긴다(§W11-1).

    - `anchor` = 이번 턴 **시작 시점의** 앵커. 세그먼트 전체(= 리셋 판정이 본 범위)를 그대로 넘겨야
      트리거 판정이 성립한다(head만 넘기면 영영 트리거에 못 닿는다).
    - `keep_from` = 리셋이 정한 **새 앵커**. 요약은 `keep_from - 1`까지 덮고 프롬프트에는 `keep_from`
      이후가 남아 겹침도 빈틈도 없다.
    - 리셋이 없는 턴은 부르지 않는다. checkpoint는 "버려지는 구간"에 대응하는 것이고, 리셋 없이
      만들면 어느 앵커와도 맞물리지 않는 경계가 생긴다.

    실패 정책은 `_record_memory_source`와 같다 — 쓰기 전에 나는 계약 위반(`CheckpointError`)은
    로그만 남기고 넘기고(요약 배선 버그가 대화를 죽이지 않게), DB 오류는 올려서 턴 전체를 롤백한다.
    """
    if not settings.context_checkpoint_enabled:
        return
    q = (
        select(Message)
        .where(Message.user_id == uid, Message.id >= anchor)
        .order_by(Message.id)
        .limit(settings.context_hard_msg_cap)  # _context와 같은 안전 상한
    )
    rows = (await session.execute(q)).scalars().all()
    if not rows:
        return
    try:
        await checkpoint_repo.maybe_enqueue(
            session,
            user_id=uid,
            messages=[
                # 저장 표면(placeholder) 그대로 — 요약에 실명이 들어가면 안 된다(§W11-4).
                checkpoint.SourceMessage(
                    id=m.id, sender=m.sender, kind=m.kind, content=m.content or ""
                )
                for m in rows
            ],
            keep_from_message_id=keep_from,
        )
    except checkpoint.CheckpointError as e:  # 쓰기 이전 단계에서만 발생 — 트랜잭션은 멀쩡하다
        _log.info("대화 요약 잡 건너뜀(user=%s): %s", uid, e)


# --- 토큰 누적(멱등 트랜잭션 내) ---
async def _accumulate_tokens(
    session: AsyncSession, uid: uuid.UUID, activity_date: date, consumed: int
) -> int | None:
    """원가 가중 billable 토큰을 당일 누적(원자 증분). 증분 후 총량을 RETURNING으로 돌려준다 —
    동시 distinct 요청 하에서도 응답의 tokens_used/remaining/review 판정이 실제 누적치와 일치하도록."""
    stmt = (
        pg_insert(UserDailyStats)
        .values(user_id=uid, activity_date=activity_date, tokens_used=consumed)
        .on_conflict_do_update(
            index_elements=["user_id", "activity_date"],
            set_={"tokens_used": UserDailyStats.tokens_used + consumed},
        )
        .returning(UserDailyStats.tokens_used)
    )
    return (await session.execute(stmt)).scalar()


# --- POST /chat/messages ---
async def post_message(
    session: AsyncSession,
    user_id: str,
    req,
    idempotency_key: str,
    *,
    deadline: float | None = None,
    capabilities: str | None = None,
) -> PostMessageResponse:
    """2단계 상태머신(SOMA-374) — LLM 호출 구간에 DB 트랜잭션/유저 락을 쥐지 않는다.

    Phase 1(짧은 txn+유저락, **DB 쓰기 없음**): 멱등 확인 → 게이팅 → 컨텍스트/기억 스냅샷 읽기 →
    프롬프트 조립 → 커밋(락·커넥션 해제). Phase 사이(커넥션 없음): LLM·백스톱.
    Phase 2(짧은 txn+유저락 재획득): 선발화·유저메시지·응답 커밋 + 토큰 누적 + 멱등응답 저장.

    Phase 1이 read-only라 LLM 실패/타임아웃 시 저장된 게 없어 클린 재시도된다(예약 누수·고아 없음).
    SQLAlchemy는 commit() 시 커넥션을 풀에 반납하므로 LLM await 동안 커넥션 점유가 0이다.
    """
    uid = _uid(user_id)
    now = datetime.now(timezone.utc)
    t0 = time.monotonic()
    absolute_deadline = deadline if deadline is not None else t0 + settings.agent_turn_deadline_s
    references_enabled = chat_turns.diary_reference_capable(capabilities)
    request_digest = chat_turns.request_hash(
        text_value=req.text,
        greeting_id=getattr(req, "greeting_id", None),
        diary_references=references_enabled,
    )
    await privacy.ensure_subject_active(session, uid)

    # 0) 멱등 — 같은 (유저,키) 재요청은 저장된 응답 그대로(이중 차감 방지, 유저 스코프)
    cached = await session.get(IdempotencyKey, (uid, idempotency_key))
    if cached is not None:
        if getattr(cached, "request_hash", None) is not None and cached.request_hash != request_digest:
            raise errors.AppError(
                "IDEMPOTENCY_KEY_REUSED",
                409,
                "같은 Idempotency-Key를 다른 요청에 사용할 수 없어요.",
            )
        if (
            cached.response is None
            or getattr(cached, "terminal_status", "succeeded") != "succeeded"
            or (
                getattr(cached, "response_expires_at", None) is not None
                and cached.response_expires_at <= now
            )
        ):
            raise errors.AppError(
                "IDEMPOTENCY_REPLAY_UNAVAILABLE",
                409,
                "이 요청의 재생 보존 기간이 끝났어요. 새 Idempotency-Key로 보내 주세요.",
            )
        # 비호환 행도 보존한 채 500 — 지우면 다음 재시도가 새 요청으로 실행되어
        # 메시지·토큰이 중복된다. 정리는 운영 스크립트(--delete-invalid)에서만.
        validated_cached = validate_post_message_response(
            cached.response, user_id=user_id, idempotency_key=idempotency_key
        )
        # LLM을 안 태운 순수 replay라 나머지 구간 지표는 없음(집계 오염 방지 — replay=True로 분리).
        _emit_turn_metrics(
            replay=True, total_ms=_ms(t0, time.monotonic()),
            phase1_ms=None, llm_ms=None, repair_ms=None,
            egress_ms=None, phase2_ms=None, prompt_tokens=None, cache_read_tokens=None,
            cache_write_tokens=None, cache_read_ratio=None, billable=None, lang=None,
            used_tools=None, context_ms=None,
        )
        return validated_cached

    # ===== Phase 1: 조립 + 만료 lease 확보(짧은 txn + 유저락). =====
    # 유저 직렬화 → 게이팅. 잠근 뒤 tokens_used를 읽어야 동시요청이 한도를 우회 못 함(TOCTOU).
    await _lock_user(session, uid)

    lease = await chat_turns.acquire(
        session,
        user_id=uid,
        idempotency_key=idempotency_key,
        request_digest=request_digest,
        lease_seconds=max(15.0, settings.agent_turn_deadline_s + 10.0),
    )

    g = await gating.resolve(session, user_id)
    remaining = g.entitlement["tokens_remaining"]
    if remaining is None:
        # 한도 미해석(app_config의 daily_token_limit dict 부분/불량) → 무제한으로 새지 않게 free 폴백.
        _log.warning("daily_token_limit 미해석 → free 한도로 fail-closed(user=%s)", user_id)
        remaining = max(0, settings.daily_token_limit_free - g.tokens_used)
    if remaining <= 0:
        raise errors.daily_limit_reached()

    # 커밋 전 스칼라 전량 캡처 — 커밋 후엔 ORM/세션 미접근(async MissingGreenlet 방지).
    ad = g.activity_date
    nick = g.profile.nickname  # 저장=placeholder / egress·LLM 투입=render 전 공용
    language = g.profile.language
    review_prompted_at = g.profile.review_prompted_at
    review_min = g.review_min_tokens
    tokens_used_pre = g.tokens_used
    limit = g.entitlement["daily_token_limit"]
    if not isinstance(limit, int):  # fail-closed(위 게이트와 동일 근거)
        limit = settings.daily_token_limit_free

    ctx = await session.get(ChatContext, uid)  # 대화 앵커·기억 처리 좌표 1회 로드
    anchor = ctx.anchor_message_id if ctx is not None else 0

    # 정규화 기억의 유일한 기본 주입 경로. 저장된 rendered_text를 그대로 쓰지 않고 repo가
    # active fact/insight와 forget marker를 매번 재대조한다. 재생성 지연 중에도 잊은 내용이
    # 프롬프트로 돌아오지 않는다. 손상된 projection은 과거 사본으로 폴백하지 않고 빈 기억으로 간다.
    relationship_text = ""
    try:
        relationship_text = await relationship_profile_repo.prompt_text(
            session, user_id=uid, language=language
        )
    except relationship_profile.ProfileError:
        _log.warning("관계 프로필 문서 손상(user=%s) — 빈 기억으로 진행", user_id, exc_info=True)

    # 대화 요약 checkpoint(W11) — 앵커 앞 구간을 대신하는 줄거리. 킬스위치 off면 조회 자체를 안 한다
    # (기존과 완전 동일). 앵커는 항상 `through + 1`이라(리셋이 요약 경계를 정한다) 여기서 읽은
    # 요약과 아래 대화 배열은 겹치지 않고 이어진다.
    checkpoint_summary = ""
    if settings.context_checkpoint_enabled:
        latest_checkpoint = await checkpoint_repo.load_latest(session, uid)
        checkpoint_summary = latest_checkpoint.summary if latest_checkpoint is not None else ""

    # 도구 루프 설정(W5) — read-only 구간에서 **1회** 조회해 frozen snapshot으로 들고 나간다.
    # agent phase는 DB도 settings도 다시 읽지 않는다(TTL 캐시 없음 = 두 EC2 캐시 불일치 없음).
    # 조회 실패는 잡지 않는다 — 설정 장애를 숨기지 않고 기존 Phase 1 DB 오류로 전파시킨다.
    agent_cfg = await agent_config.effective_agent_config(session)

    focus_block = await chat_references.load_focus_block(
        session, user_id=uid, current_turn_seq=lease.turn_seq
    )

    # 현재 턴 컨텍스트("지금 상황" 블록, W3) — 킬스위치 off면 조회 자체를 안 한다(기존과 완전 동일).
    # Phase 1(락+커넥션 보유) 안에서 끝내야 한다 — 커밋 뒤 LLM 구간엔 DB 커넥션 0(SOMA-374 불변식).
    # 실패해도 대화가 죽으면 안 되므로 fail-open(빈 블록)하고 경고만 남긴다.
    resident_block = ""
    context_ms = 0.0
    if settings.current_turn_context_enabled:
        t_context0 = time.monotonic()
        try:
            # 오늘 첫 대화 = 오늘 누적 토큰 0. 유저 메시지 저장(817행)과 토큰 누적(843행)이
            # 같은 Phase 2 트랜잭션이라 등가다. 명세 §W3-1 "이미 읽은 값은 재조회하지 않는다".
            is_first_today = tokens_used_pre == 0
            turn_ctx = await turn_context.build_context(
                session, g.profile, is_first_today=is_first_today, now_utc=now
            )
            resident_block = turn_context.render(turn_ctx, language)
        except Exception:  # noqa: BLE001 — 실패해도 응답을 막지 않는다(빈 블록으로 진행)
            _log.warning(
                "현재 턴 컨텍스트 조회 실패(user=%s) — 빈 블록으로 진행", user_id, exc_info=True
            )
            resident_block = ""
        context_ms = _ms(t_context0, time.monotonic())

    # 컨텍스트 조립 — 현재 유저 메시지는 아직 미저장. _context가 현재 턴을 in-memory로 붙인다.
    convo, new_anchor, lead = await _context(
        session, uid, anchor, current_text=req.text, current_date=ad
    )
    lead_texts = [m.content for m in lead]  # placeholder 저장분(문자열) — 커밋 후 ORM 미접근

    # 현재 턴 선발화(있으면) — 이번 턴 system[먼저 건넨 말]에 넣으려 읽기만. insert는 phase 2.
    greeting_content: str | None = None
    greeting_gid: uuid.UUID | None = None
    if getattr(req, "greeting_id", None):
        try:
            gid = uuid.UUID(req.greeting_id)
        except ValueError as e:
            raise errors.validation("잘못된 greeting_id예요.") from e
        gr = await session.get(Greeting, gid)
        if gr is not None and gr.user_id == uid and gr.committed_message_id is None:
            greeting_content = gr.content  # placeholder 저장분
            greeting_gid = gid

    # placeholder 저장분 → LLM 투입 전 현재 이름 렌더(히스토리에서도 최신 이름만 보임).
    # 현재 턴(raw)엔 placeholder가 없어 render는 무영향.
    for c in convo:
        c["content"] = naming.render(c["content"], nick)

    await session.commit()  # lease만 남기고 락·커넥션 해제 — 이후 LLM 구간엔 점유 0
    t_phase1 = time.monotonic()
    phase1_ms = _ms(t0, t_phase1)

    # ===== Phase 사이: 외부 호출(DB 커넥션 없음) — LLM + 백스톱 =====
    lead_all = lead_texts + ([greeting_content] if greeting_content else [])
    system = _build_system(
        language,
        nick,
        lead_all,
        summary=checkpoint_summary,
        relationship_text=relationship_text,
        current_state=resident_block,
    )
    if focus_block:
        system = [*system, focus_block]

    # Claude/OpenAI 호출(프롬프트 캐싱 + 실측 토큰 + per-request timeout).
    cache_on = settings.chat_prompt_cache_enabled
    t_llm0 = time.monotonic()
    # 도구 루프(W5)는 킬스위치·카나리를 모두 통과했을 때만 탄다. 아니면 아래 단발 호출 그대로다.
    try:
        if time.monotonic() >= absolute_deadline:
            raise TimeoutError("chat request deadline exceeded before inference")
        agent_turn = (
            await agent_runtime.run_turn(
                system, convo,
                config=agent_cfg, user_id=uid, language=language, activity_date=ad,
                user_text=req.text,
                deadline=absolute_deadline,
                local_calendar_date=now.astimezone(
                    safe_zone(getattr(g.profile, "timezone", "Asia/Seoul"))
                ).date(),
            )
            if agent_runtime.should_run(agent_cfg, uid)
            else None
        )
        if agent_turn is None:
            result = await llm.generate(
                system, convo,
                cache_messages=cache_on,
                ttl_system=settings.cache_ttl_system,
                ttl_messages=settings.cache_ttl_messages,
                timeout=min(settings.llm_timeout_s, absolute_deadline - time.monotonic()),
            )
    except BaseException:
        # 외부 호출 실패는 저장 0인 클린 재시도다. lease도 즉시 회수해 TTL만큼 막히지 않게 한다.
        await session.rollback()
        await chat_turns.release(session, user_id=uid, lease=lease)
        await session.commit()
        raise
    llm_ms = _ms(t_llm0, time.monotonic())
    if (
        agent_turn is None
        and cache_on
        # OpenAI는 자동캐시라 write가 실측이 아닌 추정값 → 이 경보는 Anthropic 전용(허위 WARN 방지).
        and llm.provider_for(result.model) == "anthropic"
        and result.cache_read_tokens == 0
        and result.cache_write_tokens == 0
        # 프리픽스가 모델 최소 임계 밑이면 캐시가 안 걸리는 게 정상(대화 초반). 그 위인데도
        # 0이면 진짜 고장(무음 실패)이다. read=write=0이므로 input_tokens = 프리픽스 전체.
        and result.input_tokens >= settings.chat_cache_min_prefix_tokens
    ):
        _log.warning(
            "프롬프트 캐시 미작동(read=write=0, input=%d) user=%s", result.input_tokens, user_id
        )

    # 백스톱 순서(ko만, 원문에 순차): 메타 프리앰블 제거 → 한자·가나 재작성 복원 → 정제 → placeholder.
    t_egress0 = time.monotonic()
    repair_ms = 0.0
    # 복원 호출이 붙을 수 있어 차감은 백스톱 뒤에. 도구 턴은 step1·step2가 이미 LlmCall로 온다.
    usage = TurnUsage(
        list(agent_turn.calls) if agent_turn is not None else [_llm_call(result, "chat")]
    )
    reply_text = agent_turn.text if agent_turn is not None else result.text
    if agent_turn is not None and agent_turn.control_intents and not reply_text:
        reply_text = i18n.pick(
            {
                "ko": "알겠어. 내가 기억하고 있던 것에서 지울게.",
                "en": "Okay. I'll remove that from what I remember.",
                "ja": "わかった。ぼくが覚えていたことから消すね。",
            },
            language,
        )
    is_ko = i18n.is_korean(language)  # 백스톱 게이팅 공용(메타 제거·외래문자 복원)
    if is_ko:
        # 메타 프리앰블(SOMA-329): 모델이 응답 앞에 라틴 문장으로 흘린 자기 판단·방침을 벗긴다.
        # 발동은 드문 이벤트라(3071건 중 2건) 제거한 접두부만 로그로 남겨 감사 가능하게 한다.
        # stripped는 reply_text의 접미부라 앞부분 = 제거된 메타(한국어 본문은 로그에 안 남김).
        stripped = text_clean.strip_leading_meta(reply_text)
        if stripped != reply_text:
            removed = reply_text[: len(reply_text) - len(stripped)]
            _log.warning(
                "메타 프리앰블 제거(egress) user=%s removed_len=%d prefix=%r",
                user_id, len(removed), removed[:120],
            )
            reply_text = stripped
    if is_ko and text_clean.has_foreign_ko(reply_text):
        # 세 번째 LLM 호출은 하드 데드라인과 최대 2회 호출 계약을 깨므로 결정적으로 제거한다.
        reply_text = text_clean.strip_foreign_ko(reply_text)
    reply_stored = naming.to_placeholder(_clean_reply(reply_text, nick, language), nick)
    egress_ms = _ms(t_egress0, time.monotonic())

    # 회계 대상 — v2 off(롤백)면 주 chat 호출만 차감하고 나머지는 계측·로그로만 남긴다.
    billed = usage if settings.turn_usage_v2_enabled else TurnUsage(usage.calls[:1])
    consumed = billed.total_billable
    totals = billed.totals
    if len(usage.calls) > 1:  # 다중 호출 턴만 로그(정상 턴 소음 방지) — 호출별 purpose·model·billable
        _log.info(
            "턴 LLM 호출 %d건 user=%s v2=%s billable=%d detail=%s",
            len(usage.calls), user_id, settings.turn_usage_v2_enabled, consumed,
            [(c.purpose, c.model, c.billable) for c in usage.calls],
        )

    # ===== Phase 2: 확정(짧은 txn + 유저락 재획득) =====
    t_phase2_0 = time.monotonic()
    await _lock_user(session, uid)
    await chat_turns.verify_publish(session, user_id=uid, lease=lease)
    lang_bucket = i18n.resolve(language)
    used_tools = any(c.purpose in ("tool_decide", "tool_final") for c in usage.calls)
    usage_totals = usage.totals  # W2 계측 — billed(v2 킬스위치 영향)와 별개로 실제 턴 합계
    prompt_tokens = (
        usage_totals["input_tokens"]
        + usage_totals["cache_read_tokens"]
        + usage_totals["cache_write_tokens"]
    )
    cache_read_ratio = (
        usage_totals["cache_read_tokens"] / prompt_tokens if prompt_tokens else None
    )
    # 동시 중복이 먼저 확정했으면 그 응답 반환(이중 LLM은 낭비지만 이중 저장 방지 — 유저락으로 직렬).
    dup = await session.get(IdempotencyKey, (uid, idempotency_key))
    if dup is not None:
        if getattr(dup, "request_hash", None) is not None and dup.request_hash != request_digest:
            raise errors.AppError(
                "IDEMPOTENCY_KEY_REUSED", 409, "같은 Idempotency-Key를 다른 요청에 사용할 수 없어요."
            )
        # rollback()은 identity map 객체를 expire한다(expire_on_commit=False와 무관) → 이후 dup.response
        # 접근이 async 암시적 재로드를 유발해 MissingGreenlet. 반드시 rollback 전에 값을 뽑는다.
        dup_response = dup.response
        await chat_turns.release(session, user_id=uid, lease=lease)
        await session.commit()
        validated_dup = validate_post_message_response(
            dup_response, user_id=user_id, idempotency_key=idempotency_key
        )
        # 이 요청 자체는 LLM을 실제로 태웠지만 저장은 동시 중복본에 밀렸다 — replay=True로 분리해
        # 정상 턴 집계를 오염시키지 않되, 실비용(usage)은 관측용으로 남긴다.
        _emit_turn_metrics(
            replay=True, total_ms=_ms(t0, time.monotonic()),
            phase1_ms=phase1_ms, llm_ms=llm_ms,
            repair_ms=repair_ms, egress_ms=egress_ms, phase2_ms=_ms(t_phase2_0, time.monotonic()),
            prompt_tokens=prompt_tokens, cache_read_tokens=usage_totals["cache_read_tokens"],
            cache_write_tokens=usage_totals["cache_write_tokens"], cache_read_ratio=cache_read_ratio,
            billable=usage.total_billable, lang=lang_bucket, used_tools=used_tools,
            context_ms=context_ms,
        )
        return validated_dup

    forget_results = []
    if agent_turn is not None:
        for intent in agent_turn.control_intents:
            request = memory_forget.classify(intent)
            if request is not None:
                forget_results.append(
                    await memory_forget.apply(session, user_id=uid, request=request)
                )
    if forget_results and not any(r.wrote_anything for r in forget_results):
        reply_stored = naming.to_placeholder(
            i18n.pick(
                {
                    "ko": "내가 기억하고 있던 것에서는 찾지 못했어.",
                    "en": "I couldn't find that in what I remembered.",
                    "ja": "ぼくが覚えていたことからは見つけられなかった。",
                },
                language,
            ),
            nick,
        )

    selected_refs = agent_turn.selected_refs if agent_turn is not None else ()
    focus_ref = agent_turn.focus_ref if agent_turn is not None else None
    response_mode = agent_turn.response_mode if agent_turn is not None else "summary"
    if selected_refs or focus_ref is not None:
        grounding_valid = await chat_references.validate_selected(
            session,
            user_id=uid,
            selected_refs=selected_refs,
            focus_ref=focus_ref,
        )
        if not grounding_valid:
            reply_stored = naming.to_placeholder(
                i18n.pick(
                    {
                        "ko": "그건 지금 확실하게 떠올리지 못했어.",
                        "en": "I can't recall that clearly right now.",
                        "ja": "それは今、はっきり思い出せなかった。",
                    },
                    language,
                ),
                nick,
            )
            selected_refs = ()
            focus_ref = None
            response_mode = "summary"

    # 선발화 커밋(재조회 — 여전히 유효하면). id 순서 위해 유저 메시지보다 먼저 insert.
    greeting_dto = None
    greeting_message_id: int | None = None  # 이 턴에 커밋된 선발화(기억 소스 turn에 함께 묶는다)
    if greeting_gid is not None:
        # populate_existing=True — phase-1에서 로드한 gr가 identity map + expire_on_commit=False로
        # 남아 있어, 강제 재조회 없으면 phase-1의 stale(committed_message_id=None) 상태를 본다.
        # 동일 greeting_id 동시요청이 각각 인사를 이중 커밋하는 걸 막으려면 락 하에서 fresh read 필요.
        gr = await session.get(Greeting, greeting_gid, populate_existing=True)
        if gr is not None and gr.user_id == uid and gr.committed_message_id is None:
            gmsg = Message(
                user_id=uid, sender="moly", kind="greeting", content=gr.content,
                activity_date=ad, created_at=now, turn_seq=lease.turn_seq, turn_position=0,
            )
            session.add(gmsg)
            await session.flush()
            gr.committed_message_id = gmsg.id
            greeting_message_id = gmsg.id
            # gr.content는 placeholder 저장분 → 클라 응답엔 현재 이름 렌더.
            greeting_dto = {
                "message_id": str(gmsg.id),
                "content": naming.render(gr.content, nick),
                "created_at": _iso(now),
            }

    # 유저 메시지 저장 — 유저가 자기 현재 이름을 말했으면 placeholder로(저장 표면 이름 0)
    umsg = Message(
        user_id=uid, sender="user", kind="normal",
        content=naming.to_placeholder(req.text, nick),
        activity_date=ad, created_at=now, turn_seq=lease.turn_seq, turn_position=1,
    )
    session.add(umsg)
    await session.flush()

    # 관계 시작과 welcome 프롤로그는 첫 성공 대화의 유저 메시지와 같은 트랜잭션에서 확정한다.
    if getattr(g.profile, "relationship_started_at", None) is None:
        g.profile.relationship_started_at = now
        profile_tz = getattr(g.profile, "timezone", "Asia/Seoul")
        g.profile.relationship_started_timezone = profile_tz
        g.profile.relationship_display_date = now.astimezone(safe_zone(profile_tz)).date()
        await diary_service.ensure_welcome_for_first_committed_turn(
            session, g.profile, now, source_message_id=umsg.id
        )

    if new_anchor is not None:
        await _save_anchor(session, uid, new_anchor)  # 리셋 — phase 2 원자

    # 캐피 응답 저장(+ 캐시 텔레메트리·청구 스냅샷) — 턴 내 모든 호출의 합계를 남긴다.
    rmsg = Message(
        user_id=uid, sender="moly", kind="normal",
        content=reply_stored,
        input_tokens=totals["input_tokens"], output_tokens=totals["output_tokens"],
        cache_read_tokens=totals["cache_read_tokens"],
        cache_write_tokens=totals["cache_write_tokens"],
        billable_tokens=consumed,
        activity_date=ad, created_at=now, turn_seq=lease.turn_seq, turn_position=2,
    )
    session.add(rmsg)
    await session.flush()

    # 정규화 기억 — 이 턴의 소스 turn 배정 + 추출 잡.
    # 이 턴에 커밋된 선발화도 같은 watermark에 묶는다(그 인사도 이 대화의 근거다).
    source_watermark = await _record_memory_source(
        session, uid,
        representative_message_id=umsg.id,
        message_ids=[i for i in (greeting_message_id, umsg.id, rmsg.id) if i is not None],
        now=now,
    )
    if source_watermark is not None:
        await episodic_memory.enqueue_user_message(
            session,
            user_id=uid,
            message_id=umsg.id,
            source_watermark=source_watermark,
            content=umsg.content,
        )
    await _touch_last_active(session, uid, now)

    # 대화 요약 checkpoint(W11) — 리셋이 일어난 턴에만, 앵커 저장과 같은 트랜잭션에서 잡을 건다.
    # 킬스위치 off면 no-op(현재 전 유저).
    if new_anchor is not None:
        await _enqueue_checkpoint(session, uid, anchor=anchor, keep_from=new_anchor)

    # 토큰 집계(원가 가중 billable, normal만) — 사후 누적(원자 증분). 증분 후 총량을 응답 기준으로.
    new_total = await _accumulate_tokens(session, uid, ad, consumed)
    # 원자 증분 결과가 진실. mock 등으로 None이면 phase-1 스냅샷+consumed 폴백.
    new_used = new_total if new_total is not None else tokens_used_pre + consumed
    remaining_after = max(0, limit - new_used)

    # 리뷰 노출 판정(당일 누적이 임계 생애 최초 초과 & 미노출)
    review = review_prompted_at is None and new_used >= review_min

    new_revision = await chat_turns.finish_publish(session, user_id=uid, lease=lease)
    references = await chat_references.persist_selected(
        session,
        user_id=uid,
        reply_message_id=rmsg.id,
        selected_refs=selected_refs,
        response_mode=response_mode,
        focus_ref=focus_ref,
        nickname=nick,
        context_revision=new_revision,
        turn_seq=lease.turn_seq,
        capability_enabled=references_enabled,
    )

    response = {
        "greeting": greeting_dto,
        "user_message": {"message_id": str(umsg.id), "created_at": _iso(now)},
        # 저장은 placeholder, 클라엔 현재 이름 렌더 — GET /messages도 같은 render라 화면·이력 일치.
        # M1: 렌더값을 idempotency 응답에 저장 → 멱등 리플레이도 추가 render 없이 같은 값을 돌려준다.
        "reply": {
            "message_id": str(rmsg.id),
            "content": naming.render(rmsg.content, nick),
            "created_at": _iso(now),
            "references": references,
        },
        "tokens_used": new_used,
        "tokens_remaining": remaining_after,
        "review_prompt": review,
    }

    validated = validate_post_message_response(
        response, user_id=user_id, idempotency_key=idempotency_key
    )

    # 멱등 저장 + 커밋(원자) — JSONB에는 검증 통과한 원본 dict를 저장
    from datetime import timedelta

    session.add(
        IdempotencyKey(
            user_id=uid,
            key=idempotency_key,
            request_hash=request_digest,
            response_schema_version=2,
            response=response,
            reply_message_id=rmsg.id,
            terminal_status="succeeded",
            response_expires_at=now + timedelta(hours=24),
            dedupe_expires_at=now + timedelta(days=30),
        )
    )
    await session.commit()
    t_end = time.monotonic()
    _emit_turn_metrics(
        replay=False, total_ms=_ms(t0, t_end),
        phase1_ms=phase1_ms, llm_ms=llm_ms,
        repair_ms=repair_ms, egress_ms=egress_ms, phase2_ms=_ms(t_phase2_0, t_end),
        prompt_tokens=prompt_tokens, cache_read_tokens=usage_totals["cache_read_tokens"],
        cache_write_tokens=usage_totals["cache_write_tokens"], cache_read_ratio=cache_read_ratio,
        billable=usage.total_billable, lang=lang_bucket, used_tools=used_tools,
        context_ms=context_ms,
    )
    return validated


# --- GET /chat/greeting ---
_NO_GREETING: dict[str, Any] = {"greeting_id": None, "content": None}


async def get_greeting(session: AsyncSession, user_id: str, context: str) -> dict[str, Any]:
    """선발화 = 하루(activity_date) 1회, context 무관. 없으면 빈 응답.

    캐피가 먼저 말을 거는 건 하루 한 번뿐이다. 유저가 그날 한 마디라도 했으면 더는 걸지 않는다
    (대화 중 난입 방지). 이미 낸 인사도 다시 내주지 않는다 — 재진입마다 같은 인사가
    새 말풍선으로 다시 뜨던 버그의 원인이었다. 미커밋 선발화는 원래 이력에 안 남으므로
    화면에서 사라지는 게 기존 설계와도 일관된다.
    """
    await privacy.ensure_subject_active(session, _uid(user_id))
    if context not in _GREETING_CONTEXTS:
        raise errors.validation("알 수 없는 context예요.", {"context": context})
    from app.core.time_utils import current_activity_date
    from app.services.account import _load_profile

    profile = await _load_profile(session, user_id)
    ad = current_activity_date(profile.timezone)
    uid = _uid(user_id)

    # 동시 진입(콜드스타트+푸시탭 등)이 각각 발급해 하루 2건이 되는 걸 막는다.
    await _lock_user(session, uid)

    spoke = (
        await session.execute(
            select(Message.id)
            .where(Message.user_id == uid, Message.activity_date == ad, Message.sender == "user")
            .limit(1)
        )
    ).scalars().first()
    if spoke is not None:
        await session.commit()  # 락 해제
        return dict(_NO_GREETING)

    issued = (
        await session.execute(
            select(Greeting.id)
            .where(Greeting.user_id == uid, Greeting.activity_date == ad)
            .limit(1)
        )
    ).scalars().first()
    if issued is not None:
        await session.commit()  # 락 해제
        return dict(_NO_GREETING)

    # 그날 처음 만난 시각으로 인사 톤을 고른다(home_enter만 시간대별 풀).
    hour = datetime.now(safe_zone(profile.timezone)).hour
    content = greetings.pick(context, profile.nickname, hour, profile.language)

    # 저장은 placeholder(이름 표면 0), 클라 응답엔 현재 이름 렌더.
    stored = naming.to_placeholder(content, profile.nickname)
    row = Greeting(user_id=uid, context=context, content=stored, activity_date=ad)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"greeting_id": str(row.id), "content": naming.render(stored, profile.nickname)}
