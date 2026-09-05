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

# 🔴 **현재 세대의 요약만 돌려준다.** 잊어줘는 요약을 전량 지우지만, 지우는 트랜잭션과 쓰는
# 트랜잭션이 겹치면 한쪽이 다른 쪽의 미커밋 행을 못 본다(READ COMMITTED) — 그 틈으로 들어온
# 요약은 삭제를 피한다. 잠금으로 막으면 챗 Phase 2와 락 순서가 반대라 교착이 나므로,
# **지우는 대신 읽을 때 거른다.** 끼어든 행은 남아 있어도 프롬프트에 실리지 않는다.
#
# `publish_state='published'`도 같은 이유로 여기서 거른다. 이 컬럼은 "아직 쓰면 안 되는 행"을
# 표시하는데, 거르는 조건이 없으면 표시를 붙여 넣은 행이 그대로 프롬프트에 실린다.
# 기존 행은 컬럼 기본값이 `published`라 이 조건에 영향받지 않는다.
_LATEST_SQL = text("""
SELECT c.id, c.through_message_id, c.summary, c.version, c.source_hash
FROM conversation_checkpoints c
JOIN chat_contexts cc
  ON cc.user_id = c.user_id
 AND COALESCE(cc.memory_generation, 0) = c.memory_generation
WHERE c.user_id = :user_id
  AND c.publish_state = 'published'
ORDER BY c.through_message_id DESC
LIMIT 1
""")

_COUNT_SQL = text("""
SELECT count(*)
FROM conversation_checkpoints c
JOIN chat_contexts cc
  ON cc.user_id = c.user_id
 AND COALESCE(cc.memory_generation, 0) = c.memory_generation
WHERE c.user_id = :user_id
  AND c.publish_state = 'published'
""")

# 요약 잡의 stale 판정 기준. forget이 이 값을 +1 하므로(memory_forget._BUMP_SQL), 잡을 만든
# 시점의 값과 실행 시점의 값이 다르면 그 사이에 "잊어줘"가 있었다는 뜻이다.
_GENERATION_SQL = text(
    "SELECT COALESCE(memory_generation, 0) FROM chat_contexts WHERE user_id = :user_id"
)

# 요약 대상 구간. after_id는 이전 checkpoint의 through(없으면 0) — 열린 하한, 닫힌 상한이다.
_RANGE_SQL = text("""
SELECT id, sender, kind, content
FROM messages
WHERE user_id = :user_id AND id > :after_id AND id <= :through_id
  AND kind NOT IN ('fortune_context_root','fortune_derived')
ORDER BY id
LIMIT :max_rows
""")

# 세대 검사를 **저장과 같은 문장 안에** 둔다. 따로 읽고 나서 쓰면 그 사이에 잊어줘가 끼어들어
# 세대 증가·요약 삭제를 마치고, 저장은 검사를 전부 피해 지운 요약을 되살린다(check-then-act).
#
# 한 문장이면 잠금이 필요 없다 — 잠그려 하면 챗 Phase 2와 락 순서가 반대라 교착이 나고,
# 그걸 NOWAIT로 피하면 이번엔 재시도 횟수를 갉아먹어 요약이 영구 유실된다. 검사와 행동을
# 쪼개지 않는 게 근본 해법이다.
_INSERT_SQL = text("""
INSERT INTO conversation_checkpoints
  (user_id, through_message_id, summary, version, source_hash, memory_generation)
SELECT :user_id, :through_message_id, :summary, :version, :source_hash, :expected_generation
WHERE EXISTS (
  SELECT 1 FROM chat_contexts
  WHERE user_id = :user_id AND COALESCE(memory_generation, 0) = :expected_generation
)
ON CONFLICT (user_id, through_message_id, source_hash) DO NOTHING
RETURNING id
""")

_USER_STATE_SQL = text("SELECT nickname, language FROM profiles WHERE id = :user_id")


async def read_memory_generation(session: AsyncSession, user_id: uuid.UUID | str) -> int:
    """그 유저의 현재 기억 세대. 행이 없으면 0(아직 대화 컨텍스트가 없는 유저).

    이 값은 **이른 종료용 힌트**다. 최종 방어는 `insert`가 같은 문장 안에서 하는 세대 검사이며,
    여기서 읽은 값이 낡아도 저장은 안전하다.
    """
    row = (await session.execute(_GENERATION_SQL, {"user_id": user_id})).first()
    return int(row[0]) if row is not None else 0



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
    expected_generation: int,
    version: str = checkpoint.SUMMARIZER_VERSION,
) -> uuid.UUID | None:
    """checkpoint 1행. 이미 같은 `(user, through, source_hash)`가 있으면 None(멱등).

    `expected_generation`이 지금 값과 다르면 **아무것도 쓰지 않고** None을 돌려준다 —
    그 사이에 잊어줘가 있었다는 뜻이고, 그 대화를 다시 요약해 넣으면 안 된다.
    """
    row = (
        await session.execute(
            _INSERT_SQL,
            {
                "user_id": user_id,
                "through_message_id": through_message_id,
                "summary": summary,
                "version": version,
                "source_hash": source_hash,
                "expected_generation": expected_generation,
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
    reset_triggered: bool = False,
) -> uuid.UUID | None:
    """세그먼트가 트리거에 닿았으면 요약 잡을 건다. 아니면(또는 킬스위치 off면) None.

    `messages`는 **현재 세그먼트 전체**(앵커 이후 + 이번 턴에 insert한 메시지)다. head만 넘기면
    트리거 판정이 tail을 못 봐서 영영 닿지 않는다.

    `keep_from_message_id`는 보존 tail의 첫 메시지 = **새 앵커**다(챗 배선은 항상 넘긴다).
    `checkpoint.plan` docstring 참조 — 요약 경계와 프롬프트에 남는 구간이 이 값으로 맞물린다.

    `reset_triggered=True`면 호출측이 필터 전 세그먼트로 이미 리셋을 확정했다는 뜻이다. 이때는
    필터 후 메시지가 자체 트리거에 못 미쳐도 같은 `keep_from_message_id` 앞의 정상 메시지를
    checkpoint로 보존한다.
    """
    if not settings.context_checkpoint_enabled:  # 킬스위치 — 켜기 전까지 동작 변화 0
        return None
    if not messages:
        return None
    previous = await load_latest(session, user_id)
    # 잡을 만드는 시점의 세대를 payload에 박는다. forget이 이 값을 +1 하므로, forget 이후에
    # 뒤늦게 실행된 잡은 핸들러에서 stale로 끊긴다 — 안 그러면 지운 대화를 다시 요약해 넣는다.
    generation = await read_memory_generation(session, user_id)
    plan = checkpoint.plan(
        messages,
        previous=previous,
        keep_from_message_id=keep_from_message_id,
        memory_generation=generation,
        reset_triggered=reset_triggered,
    )
    if plan is None:
        return None
    return await enqueue_checkpoint(session, user_id=user_id, plan=plan)
