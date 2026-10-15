"""탈퇴 잔여 sweep — 사용자 잡 없이 장벽 행만 보고 계정 삭제를 끝낸다(13.3절).

계정을 지우면 그 사용자의 잡(`async_jobs.user_id`)은 프로필과 함께 CASCADE로 사라진다. 그래서
삭제를 마무리하는 일은 사용자 잡으로 이어갈 수 없고, 이 잡이 user_id 없이 틱마다 돈다.
대상은 **프로필도 인증 계정도 없는** 장벽뿐이다. 살아 있는 계정은 어떤 단계에서도 바꾸지 않는다.

1. 고아 승격 — 계정이 없는데 `active`인 장벽은 장벽 없이 지워진 계정이다(moly-auth는 장벽
   시작이 실패해도 탈퇴를 진행한다). 새 삭제 회차를 열어 `deleting`으로 올린다.
2. 잔여 정리 — FK 밖 저장소(`vecs.moly_memories_v2`)를 bounded로 지운다.
3. 완료 — **연속 두 번** 비어 있어야 `mark_subject_deleted`. 늦게 도착한 벡터 쓰기를 잡기 위해서다.
   빈 횟수는 회차별 ledger(`residual_sweep_empty`)로 세고, 그 사이 지운 것이 있으면 다시 센다.
4. 멈춘 탈퇴 — 계정이 살아 있는데 1시간 넘게 `deleting`이면 경고만 남긴다(사용자는 409로 막혀 있다).
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.services import jobs, privacy
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobResult

_log = logging.getLogger("moly-worker")

JOB_PRIVACY_RESIDUAL_SWEEP = "privacy_residual_sweep"

# 한 실행에서 다루는 장벽 수와 계정당 벡터 삭제 수. 남으면 다음 틱이 이어간다.
MAX_SUBJECTS = 200
DELETE_BATCH = 200
# 연속 몇 번 비어 있어야 완료로 보는가. 1이면 늦게 온 쓰기를 놓친다.
REQUIRED_EMPTY_SWEEPS = 2

EVENT_PROMOTED = "orphan_barrier_promoted"
EVENT_EMPTY = "residual_sweep_empty"
EVENT_DELETED = "residual_sweep_deleted"

# 계정이 실제로 지워졌다 = 프로필도 인증 계정도 없다. 하나라도 있으면 대상이 아니다.
_ACCOUNT_GONE = """
  NOT EXISTS (SELECT 1 FROM profiles p WHERE p.id = b.user_id)
  AND NOT EXISTS (SELECT 1 FROM auth.users u WHERE u.id = b.user_id)
"""

_PROMOTE_ORPHANS = text(f"""
WITH orphan AS (
  SELECT b.user_id FROM privacy_subject_barriers b
  WHERE b.state = 'active' AND {_ACCOUNT_GONE}
  ORDER BY b.user_id
  LIMIT :n
), promoted AS (
  UPDATE privacy_subject_barriers b
  SET state = 'deleting', operation_id = gen_random_uuid(), epoch = b.epoch + 1, updated_at = now()
  FROM orphan o
  WHERE b.user_id = o.user_id AND b.state = 'active'
  RETURNING b.user_id, b.operation_id
)
INSERT INTO privacy_ledger_events(operation_id, user_id, event, high_watermark)
SELECT operation_id, user_id, '{EVENT_PROMOTED}', NULL FROM promoted
""")

_SUBJECTS = text(f"""
SELECT b.user_id, b.operation_id, (
  SELECT count(*) FROM privacy_ledger_events e
  WHERE e.user_id = b.user_id AND e.operation_id = b.operation_id AND e.event = '{EVENT_EMPTY}'
    AND e.id > COALESCE((
      SELECT max(d.id) FROM privacy_ledger_events d
      WHERE d.user_id = b.user_id AND d.operation_id = b.operation_id
        AND d.event = '{EVENT_DELETED}'), 0)
) AS empty_sweeps
FROM privacy_subject_barriers b
WHERE b.state = 'deleting' AND {_ACCOUNT_GONE}
-- MAX_SUBJECTS건보다 많이 밀려 있으면 오래된 삭제부터 끝낸다(회차가 지나도 updated_at은 그대로다).
ORDER BY b.updated_at, b.user_id
LIMIT :n
""")

_DELETE_VECTORS = text("""
DELETE FROM vecs.moly_memories_v2
WHERE id IN (
  SELECT id FROM vecs.moly_memories_v2 WHERE metadata->>'user_id' = :user_id LIMIT :n
)
""")

_VECTORS_LEFT = text(
    "SELECT count(*) FROM vecs.moly_memories_v2 WHERE metadata->>'user_id' = :user_id"
)

_LEDGER = text("""
INSERT INTO privacy_ledger_events(operation_id, user_id, event, high_watermark)
VALUES (:operation_id, :user_id, :event, NULL)
""")

_STUCK = text("""
SELECT count(*) FROM privacy_subject_barriers b
WHERE b.state = 'deleting' AND b.updated_at < now() - interval '1 hour'
  AND (EXISTS (SELECT 1 FROM profiles p WHERE p.id = b.user_id)
       OR EXISTS (SELECT 1 FROM auth.users u WHERE u.id = b.user_id))
""")


async def _sweep_subject(uid: uuid.UUID, operation_id: uuid.UUID, empty_sweeps: int) -> tuple[int, str]:
    """계정 하나의 한 회차를 한 트랜잭션으로. 반환 (지운 벡터 수, swept|empty|closed|skipped).

    계정마다 커밋하므로 잡이 중간에 실패해 재시도되거나 밀린 sweep 잡이 이어 돌면 같은 틱 안에
    빈 sweep이 두 번 쌓일 수 있다. 예전 privacy_cleanup도 다음 회차를 바로 걸었으므로 "연속 두 번
    비어 있어야 끝낸다"는 규칙은 그대로다. 완료 확정은 회차로 막혀 있어 두 번 일어나지 않는다.
    """
    async with get_sessionmaker()() as session:
        res = await session.execute(_DELETE_VECTORS, {"user_id": str(uid), "n": DELETE_BATCH})
        deleted = int(res.rowcount or 0)
        left = int(await session.scalar(_VECTORS_LEFT, {"user_id": str(uid)}) or 0)
        if deleted or left:
            event, outcome = EVENT_DELETED, "swept"
        elif empty_sweeps + 1 < REQUIRED_EMPTY_SWEEPS:
            event, outcome = EVENT_EMPTY, "empty"
        else:
            event = None
            # 회차(operation_id)로 막힌 확정이다. 그 사이 장벽이 바뀌었으면 아무것도 안 한다.
            closed = await privacy.mark_subject_deleted(
                session, user_id=uid, operation_id=operation_id
            )
            outcome = "closed" if closed else "skipped"
        if event is not None:
            await session.execute(
                _LEDGER, {"operation_id": operation_id, "user_id": uid, "event": event}
            )
        await session.commit()
    return deleted, outcome


async def handle_privacy_residual_sweep(job: ClaimedJob) -> JobResult:
    async with get_sessionmaker()() as session:
        promoted = int((await session.execute(_PROMOTE_ORPHANS, {"n": MAX_SUBJECTS})).rowcount or 0)
        await session.commit()
        subjects = (await session.execute(_SUBJECTS, {"n": MAX_SUBJECTS})).all()

    vectors = 0
    outcomes = {"swept": 0, "empty": 0, "closed": 0, "skipped": 0}
    for uid, operation_id, empty_sweeps in subjects:
        deleted, outcome = await _sweep_subject(uid, operation_id, int(empty_sweeps))
        vectors += deleted
        outcomes[outcome] += 1

    async with get_sessionmaker()() as session:
        stuck = int(await session.scalar(_STUCK) or 0)

    if promoted or vectors or outcomes["closed"]:
        _log.info(
            "탈퇴 sweep — 장벽 없이 지워진 계정 %d건 승격 · 남은 벡터 %d건 삭제 · 완료 %d건",
            promoted, vectors, outcomes["closed"],
        )
    if stuck:
        _log.warning(
            "탈퇴 sweep — 계정이 남은 채 1시간 넘게 삭제 중인 장벽 %d건(사용자는 409로 막혀 있음)", stuck
        )
    return JobResult(
        result_code="ok",
        result_detail={
            "promoted": promoted, "subjects": len(subjects), "vectors_deleted": vectors,
            **outcomes, "stuck": stuck,
        },
    )


async def enqueue_sweep(session: AsyncSession, *, bucket: str) -> uuid.UUID | None:
    """틱이 부른다. `bucket`이 dedup 단위라 같은 창에서는 한 번만 생긴다. 커밋은 호출측."""
    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_MAINTENANCE,
        job_type=JOB_PRIVACY_RESIDUAL_SWEEP,
        dedup_key=f"privsweep:{bucket}",
        payload={},
    )


consumer.register(JOB_PRIVACY_RESIDUAL_SWEEP, handle_privacy_residual_sweep)
