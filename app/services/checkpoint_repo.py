"""대화 요약 checkpoint(W11)의 **저장소** — SQL과 잡 연동. 순수 로직은 `checkpoint`.

여기서 지키는 것:

1. **커밋하지 않는다.** 전부 호출측 트랜잭션 경계에 합류한다 — producer는 메시지 insert와 같은
   트랜잭션에서 잡을 걸고(§W11-1), handler는 fenced finalize와 같은 트랜잭션에서 checkpoint를 쓴다.
2. **insert는 `ON CONFLICT DO NOTHING`.** UNIQUE(user_id, through_message_id, source_hash)가
   같은 입력의 두 번째 저장을 흡수한다(잡 재실행·중복 소비 멱등).
3. **범위 조회는 그 유저 것만.** 요약 입력에 남의 메시지가 섞이는 경로를 만들지 않는다.
4. **Summary는 Fact가 아니다** — 이 모듈에는 기억 추출 잡을 거는 함수가 없다(§W11-7). 요약 결과에서
   `memory_extract`를 부르는 경로를 만들면 W8의 evidence 계약이 요약으로 오염된다.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import checkpoint, jobs

_LATEST_SQL = text("""
SELECT id, through_message_id, summary, version, source_hash
FROM conversation_checkpoints
WHERE user_id = :user_id
ORDER BY through_message_id DESC
LIMIT 1
""")

_COUNT_SQL = text("SELECT count(*) FROM conversation_checkpoints WHERE user_id = :user_id")

# 요약 대상 구간. after_id는 이전 checkpoint의 through(없으면 0) — 열린 하한, 닫힌 상한이다.
_RANGE_SQL = text("""
SELECT id, sender, kind, content
FROM messages
WHERE user_id = :user_id AND id > :after_id AND id <= :through_id
ORDER BY id
LIMIT :max_rows
""")

_INSERT_SQL = text("""
INSERT INTO conversation_checkpoints
  (user_id, through_message_id, summary, version, source_hash)
VALUES (:user_id, :through_message_id, :summary, :version, :source_hash)
ON CONFLICT (user_id, through_message_id, source_hash) DO NOTHING
RETURNING id
""")

_USER_STATE_SQL = text("SELECT nickname, language FROM profiles WHERE id = :user_id")


async def load_latest(
    session: AsyncSession, user_id: uuid.UUID | str
) -> checkpoint.Checkpoint | None:
    """`through_message_id`가 가장 큰 checkpoint 하나(§W11-5). 없으면 None."""
    row = (await session.execute(_LATEST_SQL, {"user_id": user_id})).first()
    if row is None:
        return None
    return checkpoint.Checkpoint(
        id=row[0],
        through_message_id=int(row[1]),
        summary=row[2],
        version=row[3],
        source_hash=row[4],
    )


async def count(session: AsyncSession, user_id: uuid.UUID | str) -> int:
    """그 유저의 checkpoint 수 — 몇 번째 checkpoint인지(재검증 주기 판정)에 쓴다."""
    row = (await session.execute(_COUNT_SQL, {"user_id": user_id})).first()
    return int(row[0]) if row is not None else 0


async def load_user_state(
    session: AsyncSession, user_id: uuid.UUID | str
) -> tuple[str | None, str | None] | None:
    """`(nickname, language)`. 행이 없으면 None(탈퇴) — 호출측이 cancelled로 끝낸다."""
    row = (await session.execute(_USER_STATE_SQL, {"user_id": user_id})).first()
    return None if row is None else (row[0], row[1])


async def load_range(
    session: AsyncSession,
    user_id: uuid.UUID | str,
    *,
    after_id: int | None,
    through_id: int,
    max_rows: int | None = None,
) -> list[checkpoint.SourceMessage]:
    """`(after_id, through_id]` 구간 메시지 — **저장 표면 그대로**(placeholder, 실명 렌더 금지).

    `max_rows`는 폭주 방어 상한이다. 상한에 정확히 닿았는지로 "잘렸는지"를 호출측이 판정한다.
    """
    rows = (
        await session.execute(
            _RANGE_SQL,
            {
                "user_id": user_id,
                "after_id": after_id or 0,
                "through_id": through_id,
                "max_rows": max_rows or settings.context_hard_msg_cap,
            },
        )
    ).mappings().all()
    return [
        checkpoint.SourceMessage(
            id=int(r["id"]), sender=r["sender"], kind=r["kind"], content=r["content"] or ""
        )
        for r in rows
    ]


async def insert(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    through_message_id: int,
    summary: str,
    source_hash: str,
    version: str = checkpoint.SUMMARIZER_VERSION,
) -> uuid.UUID | None:
    """checkpoint 1행. 이미 같은 `(user, through, source_hash)`가 있으면 None(멱등)."""
    row = (
        await session.execute(
            _INSERT_SQL,
            {
                "user_id": user_id,
                "through_message_id": through_message_id,
                "summary": summary,
                "version": version,
                "source_hash": source_hash,
            },
        )
    ).first()
    return row[0] if row is not None else None


async def enqueue_checkpoint(
    session: AsyncSession, *, user_id: uuid.UUID | str, plan: checkpoint.CheckpointPlan
) -> uuid.UUID | None:
    """요약 잡 등록(커밋하지 않는다 — 메시지 insert와 **같은 트랜잭션**에서 불러야 한다).

    dedup key가 `(through, source_hash, summarizer)`를 담아 같은 입력은 한 번만 돈다(§W11-3).
    """
    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT,
        user_id=user_id,
        dedup_key=plan.dedup_key(user_id),
        payload=plan.payload(),
    )


async def maybe_enqueue(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    messages: Sequence[checkpoint.SourceMessage],
    keep_from_message_id: int | None = None,
) -> uuid.UUID | None:
    """세그먼트가 트리거에 닿았으면 요약 잡을 건다. 아니면(또는 킬스위치 off면) None.

    `messages`는 **현재 세그먼트 전체**(앵커 이후 + 이번 턴에 insert한 메시지)다. head만 넘기면
    트리거 판정이 tail을 못 봐서 영영 닿지 않는다.

    `keep_from_message_id`는 보존 tail의 첫 메시지 = **새 앵커**다(챗 배선은 항상 넘긴다).
    `checkpoint.plan` docstring 참조 — 요약 경계와 프롬프트에 남는 구간이 이 값으로 맞물린다.
    """
    if not settings.context_checkpoint_enabled:  # 킬스위치 — 켜기 전까지 동작 변화 0
        return None
    if not messages:
        return None
    previous = await load_latest(session, user_id)
    plan = checkpoint.plan(
        messages, previous=previous, keep_from_message_id=keep_from_message_id
    )
    if plan is None:
        return None
    return await enqueue_checkpoint(session, user_id=user_id, plan=plan)
