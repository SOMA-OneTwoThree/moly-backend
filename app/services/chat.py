"""chat 서비스 — 상태·이력·전송·선발화. 대화는 HTTP 완성본(스트리밍 없음)."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.models.chat_context import ChatContext
from app.models.greeting import Greeting
from app.models.idempotency_key import IdempotencyKey
from app.models.message import Message
from app.models.user_daily_stats import UserDailyStats
from app.services import gating, greetings, llm, memory
from app.services.account import _uid
from app.services.prompts import system_prompt

_GREETING_CONTEXTS = greetings.CONTEXTS
_log = logging.getLogger("moly-backend")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# --- GET /chat/state ---
async def get_state(session: AsyncSession, user_id: str) -> dict[str, Any]:
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
def _msg_dto(m: Message) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "sender": m.sender,
        "content": m.content,
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
) -> dict[str, Any]:
    uid = _uid(user_id)
    limit = max(1, min(limit, 100))
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

    return {
        "data": [_msg_dto(m) for m in rows],
        "older_cursor": str(rows[0].id) if rows else None,
        "newer_cursor": str(rows[-1].id) if rows else None,
    }


# --- 프롬프트용 컨텍스트(앵커 append-only) ---
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
    session: AsyncSession, uid: uuid.UUID, anchor: int
) -> tuple[list[dict[str, str]], int | None, list[Message]]:
    """앵커 이후 메시지로 대화 컨텍스트 조립. 세그먼트가 트리거 넘으면 새 앵커 반환(리셋).

    프리픽스는 리셋 때만 1회 바뀌고 그 사이엔 append-only → 캐시 히트 유지.

    셋째 반환값 = 대화 배열 맨 앞에서 밀려난 캐피 메시지(=커밋된 선발화).
    Anthropic이 messages[0]를 user로 강제해서 배열엔 못 넣지만, 버리면 캐피가 방금 건넨
    인사를 모른 채 또 인사한다. 호출측이 system 가변 블록으로 넘긴다.
    """
    q = (
        select(Message)
        .where(Message.user_id == uid, Message.id >= anchor)
        .order_by(Message.id.desc())
        .limit(settings.context_hard_msg_cap)  # 안전 상한(정상 시 리셋 트리거가 먼저 걸림)
    )
    rows = list(reversed((await session.execute(q)).scalars().all()))

    new_anchor: int | None = None
    over_msgs = len(rows) >= settings.context_reset_messages
    over_chars = sum(len(m.content or "") for m in rows) >= settings.context_reset_chars
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
    if not convo:  # 빈 배열=400. 최신 user 메시지 1개 폴백(방금 flush된 umsg가 보장)
        lead = []
        for m in reversed(rows):
            if m.sender != "moly":
                convo = [{"role": "user", "content": m.content}]
                break
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


async def _save_memory(session: AsyncSession, uid: uuid.UUID, text_: str, now: datetime) -> None:
    """기억 스냅샷 갱신 — memory 컬럼만(앵커 클로버 금지)."""
    stmt = pg_insert(ChatContext).values(
        user_id=uid, memory_text=text_, memory_refreshed_at=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id"],
        set_={"memory_text": text_, "memory_refreshed_at": now, "updated_at": func.now()},
    )
    await session.execute(stmt)


async def _resolve_memory(
    session: AsyncSession, uid: uuid.UUID, ctx: ChatContext | None, now: datetime
) -> str:
    """기억 텍스트 해결 — 신선한 스냅샷이면 그대로(핫패스 mem0 없음 + system[1] 안정→캐시 유지).

    오래됐으면 mem0 1회 재로드(6h당 1회 수준). 장애면 스냅샷 재사용(48h), 초과면 "".
    성공-빈결과가 기존 non-empty 스냅샷을 단발로 덮지 않게 함(전이 위장 방어).
    """
    refreshed = ctx.memory_refreshed_at if ctx is not None else None
    prev = ctx.memory_text if ctx is not None else None
    if refreshed is not None:
        age_h = (now - refreshed).total_seconds() / 3600
        if age_h < settings.memory_snapshot_refresh_hours:
            return prev or ""  # 신선 → 그대로
    try:
        fresh = await memory.load_for_context(str(uid))
    except memory.MemoryUnavailable:
        if prev and refreshed is not None:
            age_h = (now - refreshed).total_seconds() / 3600
            if age_h < settings.memory_snapshot_stale_hours:
                return prev  # 장애 — 최근 스냅샷 재사용
        return ""  # 장애 + 스냅샷 없음/너무 오래됨
    if not fresh and prev:
        return prev  # 빈 성공이 좋은 스냅샷을 덮지 않게(다음 턴 재시도) — 갱신 스킵
    await _save_memory(session, uid, fresh, now)
    return fresh


def _build_system(
    language: str, nickname: str | None, mem: str, lead: list[Message] | None = None
) -> list[str]:
    """system을 [페르소나(불변), 닉네임+선발화+기억(가변)] 블록으로. 뒤 블록이 바뀌어도 페르소나 캐시 생존.

    lead = 대화 배열에 못 넣은 선발화(_context 참조). 앵커가 전진하기 전까지 매 턴 같은 값이라
    가변 블록도 그대로 유지된다 — 캐시가 추가로 깨지지 않는다.
    """
    parts: list[str] = []
    if nickname:
        # 조사는 받침에 맞춰(승민이야 / 지호야) — 지시문이 틀리면 캐피도 따라 틀린다.
        parts.append(f"[상대]\n지금 얘기하는 사람 이름은 {greetings.copula(nickname)}.")
    if lead:
        said = "\n".join(m.content for m in lead if m.content)
        parts.append(
            "[먼저 건넨 말]\n"
            "이 대화 직전에 네가 먼저 말을 걸었어. 상대는 그걸 보고 답한 거야. "
            "같은 인사를 또 하지 마.\n"
            f"{said}"
        )
    if mem:
        parts.append(f"[기억]\n{mem}")
    dyn = "\n\n".join(parts)
    return [system_prompt(language), dyn] if dyn else [system_prompt(language)]


_ELLIPSIS = re.compile(r"\.{2,}|…+")  # ".." "..." / "…" (한 글자여도 말줄임표)
_WS = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([?!.,])")


def _clean_reply(text: str) -> str:
    """캐피 대사 정제 — 줄바꿈·말줄임표 제거.

    페르소나로 막아도 새서(실측 3/5) 코드로 확정한다. 채팅 말풍선은 한 덩어리 한 줄이고,
    말끝 흐리기는 캐피 톤이 아니다. 물음표·마침표·쉼표는 그대로 둔다.
    """
    out = _WS.sub(" ", _ELLIPSIS.sub(" ", text))
    return _SPACE_BEFORE_PUNCT.sub(r"\1", out).strip()


def _billable(r: llm.LLMResult) -> int:
    """실비용 가중 청구 토큰 = billable × 입력단가 = 실제 청구액(정확). 한도가 달러예산에 직결.

    write는 1.25×(read 0.1×) — cold 턴이 실제 더 비싸니 그만큼 더 셈. 30k 한도 = ~$3/월(표준가).
    """
    raw = (
        r.input_tokens
        + settings.bill_weight_output * r.output_tokens
        + settings.bill_weight_cache_read * r.cache_read_tokens
        + settings.bill_weight_cache_write * r.cache_write_tokens
    )
    return ceil(raw)


# --- 유저 단위 직렬화(토큰 한도 TOCTOU 방지) ---
async def _lock_user(session: AsyncSession, uid: uuid.UUID) -> None:
    """트랜잭션 범위 advisory lock — 같은 유저의 동시 요청을 직렬화. 커밋/롤백 시 자동 해제.
    게이팅 전에 잠가야 동시요청이 같은 pre-burst tokens_used를 읽고 한도를 우회하는 걸 막는다."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:u, 0))"), {"u": str(uid)}
    )


# --- 토큰 누적(멱등 트랜잭션 내) ---
async def _accumulate_tokens(
    session: AsyncSession, uid: uuid.UUID, activity_date: date, consumed: int
) -> None:
    stmt = pg_insert(UserDailyStats).values(
        user_id=uid, activity_date=activity_date, tokens_used=consumed
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "activity_date"],
        set_={"tokens_used": UserDailyStats.tokens_used + consumed},
    )
    await session.execute(stmt)


# --- POST /chat/messages ---
async def post_message(
    session: AsyncSession, user_id: str, req, idempotency_key: str
) -> dict[str, Any]:
    uid = _uid(user_id)
    now = datetime.now(timezone.utc)

    # 0) 멱등 — 같은 (유저,키) 재요청은 저장된 응답 그대로(이중 차감 방지, 유저 스코프)
    cached = await session.get(IdempotencyKey, (uid, idempotency_key))
    if cached is not None:
        return cached.response

    # 1) 유저 직렬화 → 게이팅. 잠근 뒤 tokens_used를 읽어야 동시요청이 한도를 우회 못 함(TOCTOU).
    await _lock_user(session, uid)

    g = await gating.resolve(session, user_id)
    remaining = g.entitlement["tokens_remaining"]
    if remaining is None:
        # 한도 미해석(app_config의 daily_token_limit dict 부분/불량) → 무제한으로 새지 않게 free 폴백.
        _log.warning("daily_token_limit 미해석 → free 한도로 fail-closed(user=%s)", user_id)
        remaining = max(0, settings.daily_token_limit_free - g.tokens_used)
    if remaining <= 0:
        raise errors.daily_limit_reached()

    ad = g.activity_date

    # 2) 선발화 커밋(있으면)
    greeting_dto = None
    if getattr(req, "greeting_id", None):
        try:
            gid = uuid.UUID(req.greeting_id)
        except ValueError as e:
            raise errors.validation("잘못된 greeting_id예요.") from e
        gr = await session.get(Greeting, gid)
        if gr is not None and gr.user_id == uid and gr.committed_message_id is None:
            gmsg = Message(
                user_id=uid, sender="moly", kind="greeting", content=gr.content,
                activity_date=ad, created_at=now,
            )
            session.add(gmsg)
            await session.flush()
            gr.committed_message_id = gmsg.id
            greeting_dto = {
                "message_id": str(gmsg.id), "content": gr.content, "created_at": _iso(now)
            }

    # 3) 유저 메시지 저장
    umsg = Message(
        user_id=uid, sender="user", kind="normal", content=req.text,
        activity_date=ad, created_at=now,
    )
    session.add(umsg)
    await session.flush()

    # 4) 컨텍스트(앵커 append-only) + 기억 스냅샷 + 시스템(페르소나/기억 블록)
    ctx = await session.get(ChatContext, uid)  # 앵커+스냅샷 1회 로드
    anchor = ctx.anchor_message_id if ctx is not None else 0
    convo, new_anchor, lead = await _context(session, uid, anchor)
    if new_anchor is not None:
        await _save_anchor(session, uid, new_anchor)  # 리셋 — 같은 트랜잭션(원자)
    mem = await _resolve_memory(session, uid, ctx, now)
    system = _build_system(g.profile.language, g.profile.nickname, mem, lead)

    # 5) Claude 호출(프롬프트 캐싱 + 실측 토큰)
    cache_on = settings.chat_prompt_cache_enabled
    result = await llm.generate(
        system, convo,
        cache_messages=cache_on,
        ttl_system=settings.cache_ttl_system,
        ttl_messages=settings.cache_ttl_messages,
    )
    if (
        cache_on
        and result.cache_read_tokens == 0
        and result.cache_write_tokens == 0
        # 프리픽스가 모델 최소 임계 밑이면 캐시가 안 걸리는 게 정상(대화 초반). 그 위인데도
        # 0이면 진짜 고장(무음 실패)이다. read=write=0이므로 input_tokens = 프리픽스 전체.
        and result.input_tokens >= settings.chat_cache_min_prefix_tokens
    ):
        _log.warning(
            "프롬프트 캐시 미작동(read=write=0, input=%d) user=%s", result.input_tokens, user_id
        )

    # 6) 캐피 응답 저장(+ 캐시 텔레메트리·청구 스냅샷)
    consumed = _billable(result)
    rmsg = Message(
        user_id=uid, sender="moly", kind="normal", content=_clean_reply(result.text),
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens, cache_write_tokens=result.cache_write_tokens,
        billable_tokens=consumed,
        activity_date=ad, created_at=now,
    )
    session.add(rmsg)
    await session.flush()

    # 7) 토큰 집계(원가 가중 billable, normal만) — 사후 누적
    await _accumulate_tokens(session, uid, ad, consumed)

    new_used = g.tokens_used + consumed
    limit = g.entitlement["daily_token_limit"]
    if not isinstance(limit, int):  # fail-closed(위 게이트와 동일 근거)
        limit = settings.daily_token_limit_free
    remaining_after = max(0, limit - new_used)

    # 8) 리뷰 노출 판정(당일 누적이 임계 생애 최초 초과 & 미노출)
    review = g.profile.review_prompted_at is None and new_used >= g.review_min_tokens

    response = {
        "greeting": greeting_dto,
        "user_message": {"message_id": str(umsg.id), "created_at": _iso(now)},
        # 저장본과 같은 값(정제 후) — 화면과 이력이 어긋나면 안 된다.
        "reply": {"message_id": str(rmsg.id), "content": rmsg.content, "created_at": _iso(now)},
        "tokens_used": new_used,
        "tokens_remaining": remaining_after,
        "review_prompt": review,
    }

    # 멱등 저장 + 커밋(원자)
    session.add(IdempotencyKey(user_id=uid, key=idempotency_key, response=response))
    await session.commit()
    return response


# --- GET /chat/greeting ---
_NO_GREETING: dict[str, Any] = {"greeting_id": None, "content": None}


async def get_greeting(session: AsyncSession, user_id: str, context: str) -> dict[str, Any]:
    """선발화 = 하루(activity_date) 1회, context 무관. 없으면 빈 응답.

    캐피가 먼저 말을 거는 건 하루 한 번뿐이다. 유저가 그날 한 마디라도 했으면 더는 걸지 않는다
    (대화 중 난입 방지). 이미 낸 인사도 다시 내주지 않는다 — 재진입마다 같은 인사가
    새 말풍선으로 다시 뜨던 버그의 원인이었다. 미커밋 선발화는 원래 이력에 안 남으므로
    화면에서 사라지는 게 기존 설계와도 일관된다.
    """
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
    hour = datetime.now(ZoneInfo(profile.timezone)).hour
    content = greetings.pick(context, profile.nickname, hour)

    row = Greeting(user_id=uid, context=context, content=content, activity_date=ad)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"greeting_id": str(row.id), "content": content}
