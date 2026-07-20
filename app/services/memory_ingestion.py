"""완료된 활동일의 대화를 mem0로 추출하는 재시도 가능 배치."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Date, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import settings
from app.models.memory_ingestion_state import MemoryIngestionState
from app.models.message import Message
from app.models.profile import Profile
from app.services import memory

_log = logging.getLogger("moly-worker")
_RETRY_DELAY = timedelta(hours=1)


@dataclass(frozen=True)
class IngestionClaim:
    state: MemoryIngestionState
    target_message_id: int


def _claim_statement(now: datetime, excluded_user_ids: set | None = None):
    state = MemoryIngestionState
    older_state = aliased(MemoryIngestionState)
    current_activity_date = cast(
        func.timezone(Profile.timezone, now) - timedelta(hours=4), Date
    )
    retry_before = now - _RETRY_DELAY
    statement = (
        select(state)
        .join(Profile, Profile.id == state.user_id)
        .where(
            state.completed_at.is_(None),
            state.attempt_count < settings.memory_ingestion_max_attempts,
            state.activity_date < current_activity_date,
            ~select(1)
            .where(
                older_state.user_id == state.user_id,
                older_state.activity_date < state.activity_date,
                older_state.completed_at.is_(None),
                older_state.attempt_count < settings.memory_ingestion_max_attempts,
            )
            .exists(),
            or_(
                state.last_attempted_at.is_(None),
                state.last_attempted_at <= retry_before,
            ),
        )
        .order_by(
            func.coalesce(state.last_attempted_at, now).asc(),
            state.activity_date.asc(),
            state.user_id.asc(),
        )
        .limit(1)
        .with_for_update(of=state, skip_locked=True)
    )
    if excluded_user_ids:
        statement = statement.where(state.user_id.not_in(excluded_user_ids))
    return statement


async def _claim_next(session: AsyncSession, now: datetime) -> IngestionClaim | None:
    excluded_user_ids: set = set()
    while True:
        state = (
            await session.execute(_claim_statement(now, excluded_user_ids))
        ).scalar_one_or_none()
        if state is None:
            return None
        user_id = state.user_id
        locked = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:u, 1))"),
            {"u": str(user_id)},
        )
        if locked:
            break
        excluded_user_ids.add(user_id)
        await session.rollback()

    target_message_id = await session.scalar(
        select(func.max(Message.id)).where(
            Message.user_id == state.user_id,
            Message.activity_date == state.activity_date,
            Message.kind == "normal",
        )
    )
    state.attempt_count += 1
    state.last_attempted_at = now
    state.completed_at = None
    await session.flush()
    return IngestionClaim(
        state=state,
        target_message_id=max(state.through_message_id, int(target_message_id or 0)),
    )


async def _messages_for_claim(
    session: AsyncSession, claim: IngestionClaim
) -> list[Message]:
    state = claim.state
    rows = await session.execute(
        select(Message)
        .where(
            Message.user_id == state.user_id,
            Message.activity_date == state.activity_date,
            Message.kind == "normal",
            Message.id > state.through_message_id,
            Message.id <= claim.target_message_id,
        )
        .order_by(Message.id.asc())
    )
    return list(rows.scalars().all())


async def _process_claim(
    session: AsyncSession, claim: IngestionClaim, now: datetime
) -> None:
    messages = await _messages_for_claim(session, claim)
    if messages:
        ingestion_key = (
            f"v1:{claim.state.activity_date.isoformat()}:{messages[0].id}:{messages[-1].id}"
        )
        await asyncio.wait_for(
            memory.add_conversation(
                str(claim.state.user_id),
                [
                    {
                        "role": "assistant" if m.sender == "moly" else "user",
                        "content": m.content,
                    }
                    for m in messages
                ],
                metadata={
                    "source": "conversation",
                    "schema_version": 1,
                    "attributed_to": "user",
                    "activity_date": claim.state.activity_date.isoformat(),
                    "message_id_start": messages[0].id,
                    "message_id_end": messages[-1].id,
                    "ingestion_key": ingestion_key,
                },
            ),
            timeout=settings.memory_ingestion_timeout_seconds,
        )

    claim.state.through_message_id = claim.target_message_id
    claim.state.completed_at = now


async def _mark_legacy_snapshot_stale(
    session: AsyncSession, user_id: str, now: datetime
) -> None:
    stale_before = now - timedelta(hours=settings.memory_snapshot_refresh_hours)
    await session.execute(
        text(
            "UPDATE chat_contexts "
            "SET memory_refreshed_at = LEAST(memory_refreshed_at, :stale_before) "
            "WHERE user_id = :u"
        ),
        {"u": user_id, "stale_before": stale_before},
    )


async def _lock_chat_snapshot(session: AsyncSession, user_id: str) -> None:
    # mem0 추출 중에는 chat을 막지 않고, snapshot read/write 구간만 chat과 직렬화한다.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:u, 0))"),
        {"u": user_id},
    )


async def _refresh_legacy_snapshot(
    session: AsyncSession, user_id: str, now: datetime
) -> None:
    snapshot = await memory.load_for_context(user_id)
    await session.execute(
        text(
            "INSERT INTO chat_contexts (user_id, memory_text, memory_refreshed_at) "
            "VALUES (CAST(:u AS uuid), :memory, :refreshed_at) "
            "ON CONFLICT (user_id) DO UPDATE "
            "SET memory_text = EXCLUDED.memory_text, "
            "    memory_refreshed_at = EXCLUDED.memory_refreshed_at, "
            "    updated_at = now()"
        ),
        {"u": user_id, "memory": snapshot, "refreshed_at": now},
    )


async def ingest_pending(
    session: AsyncSession,
    now: datetime | None = None,
    *,
    batch_size: int | None = None,
) -> int:
    """완료일 기억을 최대 batch_size건 처리한다. mem0 실패는 1시간 후 재시도한다."""
    if not settings.memory_ingestion_enabled:
        return 0
    now = now or datetime.now(timezone.utc)
    limit = settings.memory_ingestion_batch_size if batch_size is None else batch_size

    completed = 0
    for _ in range(limit):
        claim = await _claim_next(session, now)
        if claim is None:
            await session.rollback()
            break
        try:
            await _process_claim(session, claim, now)
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "기억 추출 실패(user=%s date=%s attempt=%s): %r",
                claim.state.user_id,
                claim.state.activity_date,
                claim.state.attempt_count,
                e,
            )
            try:
                await session.commit()
            except Exception:  # noqa: BLE001
                await session.rollback()
                break
            if claim.state.attempt_count >= settings.memory_ingestion_max_attempts:
                _log.error(
                    "기억 추출 재시도 소진(user=%s date=%s attempts=%s)",
                    claim.state.user_id,
                    claim.state.activity_date,
                    claim.state.attempt_count,
                )
            continue

        try:
            try:
                await _lock_chat_snapshot(session, str(claim.state.user_id))
                await _refresh_legacy_snapshot(session, str(claim.state.user_id), now)
            except memory.MemoryUnavailable as e:
                _log.warning(
                    "legacy 기억 스냅샷 갱신 실패(user=%s): %r",
                    claim.state.user_id,
                    e,
                )
                await _mark_legacy_snapshot_stale(
                    session, str(claim.state.user_id), now
                )
            await session.commit()
        except Exception as e:  # noqa: BLE001
            _log.exception(
                "기억 watermark·snapshot 커밋 실패(user=%s date=%s) — 이번 틱 중단: %r",
                claim.state.user_id,
                claim.state.activity_date,
                e,
            )
            await session.rollback()
            break
        completed += 1
    return completed
