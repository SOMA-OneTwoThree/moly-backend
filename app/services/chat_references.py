"""서버 검증된 대화 참조 카드와 짧은 후속 화제 focus."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import and_, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversational_recall import (
    ChatResponseReference,
    ConversationFocus,
    DiaryClaimSource,
    RecallSuppression,
)
from app.models.diary import Diary
from app.services import naming


_VALID_DIARIES = text("""
SELECT count(*)
FROM diaries d
WHERE d.user_id=:user_id AND d.id=ANY(CAST(:ids AS uuid[]))
  AND d.kind IN ('welcome','shared_day','capi_day')
  AND d.record_status='published' AND d.deleted_at IS NULL
  AND d.published_at IS NOT NULL AND d.published_at<=now()
  AND NOT EXISTS (
    SELECT 1 FROM diary_claim_sources s
    JOIN memory_recall_suppressions x
      ON x.user_id=s.user_id AND x.message_id=s.message_id
    WHERE s.user_id=d.user_id AND s.diary_id=d.id
  )
""")

_VALID_FACTS = text("""
SELECT count(*)
FROM memory_facts f
WHERE f.user_id=:user_id AND f.id=ANY(CAST(:ids AS uuid[])) AND f.status='active'
  AND NOT EXISTS (
    SELECT 1 FROM memory_forget_markers m
    WHERE m.user_id=f.user_id AND (
      (m.scope='all' AND (m.future_learning='block'
         OR COALESCE(f.learned_at_watermark,0)<=m.cut_watermark))
      OR (m.scope='predicate' AND m.predicate=f.predicate
         AND (m.future_learning='block'
           OR COALESCE(f.learned_at_watermark,0)<=m.cut_watermark))
      OR (m.scope='fact' AND m.normalization_version=f.normalization_version
          AND m.normalized_hash=f.content_hash
          AND (m.future_learning='block'
            OR COALESCE(f.learned_at_watermark,0)<=m.cut_watermark))
    )
  )
  AND EXISTS (
    SELECT 1 FROM memory_evidence e
    WHERE e.user_id=f.user_id AND e.fact_id=f.id AND e.source_sender='user'
      AND NOT EXISTS (
        SELECT 1 FROM memory_recall_suppressions x
        WHERE x.user_id=e.user_id AND x.message_id=e.source_id
      )
  )
""")

_VALID_EPISODES = text("""
SELECT count(*)
FROM memory_episodic_messages e
JOIN messages m ON m.user_id=e.user_id AND m.id=e.message_id AND m.sender='user'
WHERE e.user_id=:user_id AND e.message_id=ANY(CAST(:ids AS bigint[]))
  AND e.content_hash=encode(digest(COALESCE(m.content,''),'sha256'),'hex')
  AND NOT EXISTS (
    SELECT 1 FROM memory_recall_suppressions x
    WHERE x.user_id=e.user_id AND x.message_id=e.message_id
  )
""")


async def validate_selected(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    selected_refs: Iterable,
    focus_ref,
) -> bool:
    """Phase B에서 allowlist ref의 현재 소유권·공개·망각 상태를 다시 확인한다.

    runtime은 같은 턴 tool 결과에 있던 ID인지 먼저 확인한다. 이 함수는 그 뒤 tool 실행과
    최종 commit 사이에 삭제·망각된 ID가 없는지 검사한다. 하나라도 유효하지 않으면 부분
    grounding을 살리지 않고 호출측이 전체 답변을 결정적 fallback으로 바꾼다.
    """
    refs = list(selected_refs)
    if focus_ref is not None:
        refs.append(focus_ref)
    unique = {(getattr(ref, "ref_type", None), getattr(ref, "ref_id", None)) for ref in refs}
    if not unique:
        return True
    expected: dict[str, set] = {"diary": set(), "memory_fact": set(), "memory_episode": set()}
    try:
        for ref_type, ref_id in unique:
            if ref_type in {"diary", "memory_fact"}:
                expected[ref_type].add(uuid.UUID(str(ref_id)))
            elif ref_type == "memory_episode":
                expected[ref_type].add(int(str(ref_id)))
            else:
                return False
    except (TypeError, ValueError):
        return False
    checks = (
        ("diary", _VALID_DIARIES),
        ("memory_fact", _VALID_FACTS),
        ("memory_episode", _VALID_EPISODES),
    )
    for ref_type, stmt in checks:
        ids = expected[ref_type]
        if ids and int(
            await session.scalar(stmt, {"user_id": user_id, "ids": list(ids)}) or 0
        ) != len(ids):
            return False
    return True


def _card(diary: Diary, nickname: str | None) -> dict:
    return {
        "id": str(diary.id),
        "kind": diary.kind,
        "author": diary.author,
        "display_date": diary.display_date.isoformat(),
        "title": naming.render(diary.title, nickname) if diary.title else None,
        "body": naming.render(diary.content, nickname),
        "published_at": diary.published_at.isoformat(),
        "read": diary.first_read_at is not None,
    }


def _wire(ref: ChatResponseReference, diary: Diary | None, nickname: str | None) -> dict:
    available = ref.state == "available" and diary is not None
    return {
        "reference_id": str(ref.id),
        "schema": ref.schema_version,
        "type": "diary",
        "state": "available" if available else "unavailable",
        "mode": ref.mode,
        "diary": _card(diary, nickname) if available else None,
    }


async def persist_selected(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    reply_message_id: int,
    selected_refs: Iterable,
    response_mode: str,
    focus_ref,
    nickname: str | None,
    context_revision: int,
    turn_seq: int,
    capability_enabled: bool,
) -> list[dict]:
    diary_ids: list[uuid.UUID] = []
    for ref in selected_refs:
        if getattr(ref, "ref_type", None) != "diary":
            continue
        try:
            did = uuid.UUID(getattr(ref, "ref_id"))
        except (TypeError, ValueError):
            continue
        if did not in diary_ids:
            diary_ids.append(did)
    diary_ids = diary_ids[:3]
    if not diary_ids:
        await session.execute(delete(ConversationFocus).where(ConversationFocus.user_id == user_id))
        return []

    now = datetime.now(timezone.utc)
    eligible = list(
        (
            await session.execute(
                select(Diary).where(
                    Diary.user_id == user_id,
                    Diary.id.in_(diary_ids),
                    Diary.kind.in_(("welcome", "shared_day", "capi_day")),
                    Diary.record_status == "published",
                    Diary.deleted_at.is_(None),
                    Diary.published_at.is_not(None),
                    Diary.published_at <= now,
                    ~select(1)
                    .where(
                        and_(
                            DiaryClaimSource.user_id == Diary.user_id,
                            DiaryClaimSource.diary_id == Diary.id,
                            RecallSuppression.user_id == DiaryClaimSource.user_id,
                            RecallSuppression.message_id == DiaryClaimSource.message_id,
                        )
                    )
                    .exists(),
                )
            )
        ).scalars().all()
    )
    by_id = {d.id: d for d in eligible}
    ordered = [by_id[did] for did in diary_ids if did in by_id]
    if not ordered:
        await session.execute(delete(ConversationFocus).where(ConversationFocus.user_id == user_id))
        return []

    # focus는 클라이언트 capability와 무관한 서버 대화 상태다.
    focus_ids = [d.id for d in ordered]
    if focus_ref is not None and getattr(focus_ref, "ref_type", None) == "diary":
        try:
            fid = uuid.UUID(getattr(focus_ref, "ref_id"))
            if fid in by_id:
                focus_ids = [fid]
        except (TypeError, ValueError):
            pass
    stmt = pg_insert(ConversationFocus).values(
        user_id=user_id,
        domain="diary",
        facet=response_mode,
        reference_ids=focus_ids,
        context_revision=context_revision,
        expires_at=now + timedelta(minutes=15),
        expires_turn_seq=turn_seq + 6,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                "domain": "diary",
                "facet": response_mode,
                "reference_ids": focus_ids,
                "context_revision": context_revision,
                "expires_at": now + timedelta(minutes=15),
                "expires_turn_seq": turn_seq + 6,
                "updated_at": now,
            },
        )
    )
    if not capability_enabled or response_mode not in {"full_card", "reopen_reference"}:
        return []
    wire: list[dict] = []
    for ordinal, diary in enumerate(ordered):
        ref = ChatResponseReference(
            user_id=user_id,
            reply_message_id=reply_message_id,
            ordinal=ordinal,
            mode=response_mode,
            diary_id=diary.id,
            rendered_metadata={},
        )
        session.add(ref)
        await session.flush()
        wire.append(_wire(ref, diary, nickname))
    return wire


async def hydrate_for_messages(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    message_ids: list[int],
    nickname: str | None,
    capability_enabled: bool,
) -> dict[int, list[dict]]:
    if not capability_enabled or not message_ids:
        return {}
    rows = (
        await session.execute(
            select(ChatResponseReference, Diary)
            .outerjoin(
                Diary,
                and_(Diary.user_id == ChatResponseReference.user_id, Diary.id == ChatResponseReference.diary_id),
            )
            .where(
                ChatResponseReference.user_id == user_id,
                ChatResponseReference.reply_message_id.in_(message_ids),
            )
            .order_by(ChatResponseReference.reply_message_id, ChatResponseReference.ordinal)
        )
    ).all()
    out: dict[int, list[dict]] = {}
    for ref, diary in rows:
        out.setdefault(ref.reply_message_id, []).append(_wire(ref, diary, nickname))
    return out


async def load_focus_block(
    session: AsyncSession, *, user_id: uuid.UUID, current_turn_seq: int
) -> str:
    now = datetime.now(timezone.utc)
    focus = await session.get(ConversationFocus, user_id)
    if (
        focus is None
        or focus.expires_at <= now
        or focus.expires_turn_seq < current_turn_seq
        or not focus.reference_ids
    ):
        if focus is not None:
            await session.delete(focus)
        return ""
    ids = ",".join(str(value) for value in focus.reference_ids)
    return (
        "[이어지는 화제]\n직전 회상에서 이어지는 대상이야. 사용자가 '그거', '전문', "
        f"'더 말해줘'처럼 말하면 먼저 이 대상을 이어서 회상해. domain={focus.domain}; ids={ids}"
    )
