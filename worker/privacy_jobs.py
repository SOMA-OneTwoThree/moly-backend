"""삭제 coordinator — 장벽을 세운 뒤 실제로 지우고, 두 번 비어 있어야 끝낸다 (12.3절).

`begin_subject_deletion`은 장벽만 세우고 `mark_subject_deleted`는 끝났다고 표시할 뿐,
**그 사이에서 실제로 지우는 코드가 없었다**(감사 지적). 장벽만 서고 벡터는 남는 상태였다.

세 가지를 지킨다:

**bounded** — 한 번에 전량을 지우지 않는다. `delete_by_user(limit)`가 정해진 만큼만 지우고,
남으면 다음 잡으로 이어간다. 큰 계정에서 한 트랜잭션이 길어지면 그 사이 실패했을 때 어디까지
지웠는지 알 수 없다.

**two-sweep** — 0을 한 번 보고 끝내지 않는다. 늦게 도착한 쓰기가 그 뒤에 들어올 수 있으므로,
**연속 두 번** 비어 있어야 완료로 본다.

**멱등** — 같은 잡이 두 번 돌아도 안전하다. 이미 지운 것은 다시 세지 않는다.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import privacy
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobFatal, JobResult, JobRetry

_log = logging.getLogger("moly-worker")

JOB_PRIVACY_CLEANUP = "privacy_cleanup"

# 한 번에 지우는 벡터 수. 크면 트랜잭션이 길어지고 작으면 잡이 늘어난다.
DELETE_BATCH = 200
# 연속 몇 번 비어 있어야 완료로 보는가. 1이면 늦게 온 쓰기를 놓친다.
REQUIRED_EMPTY_SWEEPS = 2

_REMAINING = text("""
SELECT count(*) FROM vecs.moly_memories_v2 WHERE metadata->>'user_id' = :user_id
""")

_ROWS_REMAINING = text("""
SELECT
  (SELECT count(*) FROM mem0_memory_registry WHERE user_id = :uid) AS registry,
  (SELECT count(*) FROM mem0_ingest_candidates WHERE user_id = :uid) AS candidates,
  (SELECT count(*) FROM mem0_memory_sources WHERE user_id = :uid) AS sources
""")


async def handle_privacy_cleanup(job: ClaimedJob) -> JobResult:
    """장벽이 선 사용자의 벡터를 bounded로 지운다. 남으면 다음 잡을 만든다."""
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id
    payload = job.payload or {}
    empty_sweeps = int(payload.get("empty_sweeps", 0))

    async with get_sessionmaker()() as session:
        barrier = await privacy.load_barrier(session, uid)
    if barrier.status != "deleting":
        # 장벽이 없거나 이미 끝났다. 지울 근거가 없으므로 아무것도 하지 않는다.
        return JobResult(result_code="not_deleting")

    from worker.mem0_jobs import _adapter

    try:
        deleted = await _adapter().delete_by_user(str(uid), limit=DELETE_BATCH)
    except Exception as e:  # noqa: BLE001  provider 장애 — 재시도
        raise JobRetry("delete_failed") from e

    async with get_sessionmaker()() as session:
        remaining = int(await session.scalar(_REMAINING, {"user_id": str(uid)}) or 0)
        rows = (await session.execute(_ROWS_REMAINING, {"uid": uid})).first()

    # 이번 sweep이 비었나. 지운 것도 없고 남은 것도 없어야 빈 것이다.
    swept_clean = deleted == 0 and remaining == 0
    next_sweeps = empty_sweeps + 1 if swept_clean else 0

    async def _apply(session) -> None:
        if next_sweeps >= REQUIRED_EMPTY_SWEEPS:
            # 두 번 연속 비었다 — 이제 끝났다고 표시한다.
            await privacy.mark_subject_deleted(session, uid)
            _log.info("삭제 완료 — user=%s (registry 잔여 %s)", uid, dict(rows._mapping) if rows else {})
            return
        # 아직이다. 다음 sweep을 건다. dedup_key에 회차를 넣어 매번 새 잡이 된다.
        from app.services import jobs

        await jobs.enqueue(
            session,
            queue=jobs.QUEUE_MAINTENANCE,
            job_type=JOB_PRIVACY_CLEANUP,
            user_id=uid,
            dedup_key=f"privclean:{uid}:{barrier.epoch}:{empty_sweeps + 1}",
            payload={"empty_sweeps": next_sweeps, "privacy_epoch": barrier.epoch},
        )

    return JobResult(
        result_code="ok",
        result_detail={
            "deleted": deleted, "remaining": remaining,
            "empty_sweeps": next_sweeps,
            "done": next_sweeps >= REQUIRED_EMPTY_SWEEPS,
        },
        apply_domain=_apply,
    )


consumer.register(JOB_PRIVACY_CLEANUP, handle_privacy_cleanup)
