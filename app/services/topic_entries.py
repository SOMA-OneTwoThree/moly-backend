"""Authenticated preparation and idempotent recovery; no model calls."""

import hashlib
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.advisory_lock import advisory_xact_lock
from app.core.app_day import validate_app_timezone
from app.core.errors import AppError
from app.models.chat_context import ChatContext
from app.models.idempotency_key import IdempotencyKey, TOPIC_PREPARE_KEY_PREFIX
from app.models.topic import ChatTopicEntry, UserTopicState
from app.schemas.topics import PrepareTopicRequest, entry_response
from app.services import privacy
from app.services.account import _load_profile
from app.services.banner_catalog import BannerCatalog, select_candidates
from app.services.topic_catalog import TopicCatalog
from app.services.topic_state import PLACEMENT, prepare_entry


async def prepare(
    session: AsyncSession, user_id: str, req: PrepareTopicRequest, key: str,
    *, banner_catalog: BannerCatalog | None, topic_catalog: TopicCatalog | None,
    now: datetime, timezone_name: str | None,
):
    validate_app_timezone(timezone_name)
    uid = uuid.UUID(user_id)
    digest = hashlib.sha256(json.dumps(
        {"request": req.model_dump(mode="json"), "timezone": timezone_name},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    await advisory_xact_lock(session, uid)
    await privacy.ensure_subject_active(session, uid)
    cached = await session.get(IdempotencyKey, (uid, TOPIC_PREPARE_KEY_PREFIX + key))
    context = await session.get(ChatContext, uid, populate_existing=True)
    revision = context.context_revision if context else 0
    if cached is not None:
        if cached.request_hash != digest:
            raise AppError("IDEMPOTENCY_CONFLICT", 409, "같은 요청 키의 내용이 달라졌어요.")
        entry_id = (cached.response or {}).get("entry_id")
        entry = await session.scalar(select(ChatTopicEntry).where(
            ChatTopicEntry.id == uuid.UUID(entry_id), ChatTopicEntry.user_id == uid,
        )) if entry_id else None
        if entry is None:
            raise AppError("TOPIC_ENTRY_UNAVAILABLE", 409, "대화 주제를 다시 열어 주세요.")
        if entry.state == "pending" and (
            entry.context_revision != revision or topic_catalog is None
            or not topic_catalog.manifest.enabled
            or topic_catalog.is_revoked(entry.topic_id, entry.topic_revision)
        ):
            entry.state = "superseded"
        return entry_response(entry, locale=req.topic_ref.locale, now=now)
    if banner_catalog is None or topic_catalog is None:
        raise AppError("TOPICS_UNAVAILABLE", 503, "대화 주제를 불러올 수 없습니다.")
    candidates = select_candidates(
        banner_catalog, now=now, platform=req.platform, app_version=req.app_version,
        locale=req.topic_ref.locale, supported=frozenset(req.capabilities),
    )
    if not any(b.id == req.banner_id and locale == req.topic_ref.locale and any(
        getattr(getattr(e, "action", None), "type", None) == "open_topic_conversation_v1"
        for e in canvas.elements
    ) for b, locale, canvas in candidates):
        raise AppError("TOPIC_OFFER_UNAVAILABLE", 409, "새 대화 주제를 확인해 주세요.")
    state = await session.get(UserTopicState, (uid, PLACEMENT), populate_existing=True)
    ref = req.topic_ref
    if state is None or (
        state.offer_id, state.offer_sequence, state.topic_id, state.topic_revision
    ) != (ref.offer_id, ref.offer_sequence, ref.topic_id, ref.topic_revision):
        # A successful first opening already moved the banner to B. A second
        # device or a lost response may still be restoring the prepared A.
        pending = await session.scalar(select(ChatTopicEntry).where(
            ChatTopicEntry.user_id == uid, ChatTopicEntry.offer_id == ref.offer_id,
            ChatTopicEntry.offer_sequence == ref.offer_sequence,
            ChatTopicEntry.topic_id == ref.topic_id,
            ChatTopicEntry.topic_revision == ref.topic_revision,
            ChatTopicEntry.state == "pending",
            ChatTopicEntry.context_revision == revision,
            ChatTopicEntry.expires_at > now,
        ))
        if pending is None:
            raise AppError("TOPIC_OFFER_UNAVAILABLE", 409, "새 대화 주제를 확인해 주세요.")
    active = await session.scalar(text(
        "SELECT 1 FROM chat_active_turns WHERE user_id=:uid AND lease_until > now()"
    ), {"uid": uid})
    if active:
        raise AppError("CHAT_TURN_IN_PROGRESS", 409, "앞선 답변을 마무리하고 있어요.",
                       {"retry_after_seconds": 2})
    if timezone_name is None:
        timezone_name = (await _load_profile(session, user_id)).timezone
    entry = await prepare_entry(session, uid, ref.offer_id, topic_catalog,
                                now, timezone_name, revision)
    session.add(IdempotencyKey(
        user_id=uid, key=TOPIC_PREPARE_KEY_PREFIX + key, request_hash=digest,
        response={"entry_id": str(entry.id)}, response_schema_version=1,
        terminal_status="succeeded", response_expires_at=now + timedelta(days=30),
        dedupe_expires_at=now + timedelta(days=30), created_at=now,
    ))
    await session.flush()
    return entry_response(entry, locale=ref.locale, now=now)
