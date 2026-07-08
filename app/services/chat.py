"""chat 서비스 — 상태·이력·전송·선발화. 대화는 HTTP 완성본(스트리밍 없음)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.models.greeting import Greeting
from app.models.idempotency_key import IdempotencyKey
from app.models.message import Message
from app.models.user_daily_stats import UserDailyStats
from app.services import gating, greetings, llm, memory
from app.services.account import _uid
from app.services.prompts import system_prompt

_GREETING_CONTEXTS = greetings.CONTEXTS


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


# --- 프롬프트용 최근 대화 ---
async def _recent_convo(session: AsyncSession, uid: uuid.UUID) -> list[dict[str, str]]:
    q = (
        select(Message)
        .where(Message.user_id == uid)
        .order_by(Message.id.desc())
        .limit(settings.chat_recent_messages)
    )
    rows = list(reversed((await session.execute(q)).scalars().all()))
    convo = [
        {"role": "assistant" if m.sender == "moly" else "user", "content": m.content}
        for m in rows
    ]
    while convo and convo[0]["role"] != "user":  # Anthropic: 첫 메시지 user 보장
        convo.pop(0)
    return convo


async def _build_system(user_id: str, language: str, nickname: str | None = None) -> str:
    mem = await memory.load_for_context(user_id)
    system = system_prompt(language)
    if nickname:
        system += f"\n\n[상대]\n지금 얘기하는 사람 이름은 '{nickname}'야."
    if mem:
        system += f"\n\n[기억]\n{mem}"
    return system


# --- 유저 단위 직렬화(토큰 한도 TOCTOU 방지) ---
async def _lock_user(session: AsyncSession, uid: uuid.UUID) -> None:
    """트랜잭션 범위 advisory lock — 같은 유저의 동시 요청을 직렬화. 커밋/롤백 시 자동 해제.
    게이팅 전에 잠가야 동시요청이 같은 pre-burst tokens_used를 읽고 한도를 우회하는 걸 막는다.
    uid는 검증된 UUID라 리터럴 삽입 안전(bind 파라미터 없이 단일 execute)."""
    await session.execute(text(f"SELECT pg_advisory_xact_lock(hashtextextended('{uid}', 0))"))


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
    if remaining is not None and remaining <= 0:
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

    # 4) 컨텍스트(최근 N + 장기기억)
    system = await _build_system(user_id, g.profile.language, g.profile.nickname)
    convo = await _recent_convo(session, uid)

    # 5) Claude 호출(완성본 + 실측 토큰)
    result = await llm.generate(system, convo)

    # 6) 바라 응답 저장
    rmsg = Message(
        user_id=uid, sender="moly", kind="normal", content=result.text,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        activity_date=ad, created_at=now,
    )
    session.add(rmsg)
    await session.flush()

    # 7) 토큰 집계(normal만) — 사후 누적
    consumed = result.input_tokens + result.output_tokens
    await _accumulate_tokens(session, uid, ad, consumed)

    new_used = g.tokens_used + consumed
    limit = g.entitlement["daily_token_limit"]
    remaining_after = max(0, limit - new_used) if isinstance(limit, int) else None

    # 8) 리뷰 노출 판정(당일 누적이 임계 생애 최초 초과 & 미노출)
    review = g.profile.review_prompted_at is None and new_used >= g.review_min_tokens

    response = {
        "greeting": greeting_dto,
        "user_message": {"message_id": str(umsg.id), "created_at": _iso(now)},
        "reply": {"message_id": str(rmsg.id), "content": result.text, "created_at": _iso(now)},
        "tokens_used": new_used,
        "tokens_remaining": remaining_after,
        "review_prompt": review,
    }

    # 멱등 저장 + 커밋(원자)
    session.add(IdempotencyKey(user_id=uid, key=idempotency_key, response=response))
    await session.commit()
    return response


# --- GET /chat/greeting ---
async def get_greeting(session: AsyncSession, user_id: str, context: str) -> dict[str, Any]:
    if context not in _GREETING_CONTEXTS:
        raise errors.validation("알 수 없는 context예요.", {"context": context})
    from app.services.account import _load_profile

    profile = await _load_profile(session, user_id)
    from app.core.time_utils import current_activity_date

    ad = current_activity_date(profile.timezone)
    uid = _uid(user_id)

    # 캐시: 같은 context·activity_date면 동일 건 반환(LLM 재호출 없음, 미차감)
    existing = (
        await session.execute(
            select(Greeting).where(
                Greeting.user_id == uid,
                Greeting.context == context,
                Greeting.activity_date == ad,
            )
        )
    ).scalars().first()
    if existing is not None:
        return {"greeting_id": str(existing.id), "content": existing.content}

    content = greetings.pick(context, profile.nickname)

    row = Greeting(user_id=uid, context=context, content=content, activity_date=ad)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"greeting_id": str(row.id), "content": content}
