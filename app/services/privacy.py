"""계정 삭제 오케스트레이터가 호출하는 moly-backend 측 삭제 장벽.

인증 계정 삭제 자체는 moly-auth 소유다. 그 오케스트레이터는 프로필을 지우기 전에
``begin_subject_deletion``을 같은 DB에 적용해야 한다. 이후 worker publish가 차단되고 응답 사본과
잡 payload가 즉시 비식별화된다. 외부 삭제 완료 후 ``mark_subject_deleted``로 감사 좌표를 닫는다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors


_BEGIN = text("""
INSERT INTO privacy_subject_barriers(user_id,state,operation_id,high_watermark)
SELECT :user_id,'deleting',:operation_id,c.memory_source_watermark
FROM chat_contexts c WHERE c.user_id=:user_id
ON CONFLICT (user_id) DO UPDATE SET
  state='deleting',operation_id=EXCLUDED.operation_id,
  -- 삭제 시작마다 세대를 올린다. 이전 epoch의 pending/running 잡은 authorize_job에서 걸린다.
  epoch=privacy_subject_barriers.epoch + 1,
  high_watermark=GREATEST(privacy_subject_barriers.high_watermark,EXCLUDED.high_watermark),
  updated_at=now()
RETURNING high_watermark
""")

_REDACT = text("""
WITH idem AS (
  UPDATE idempotency_keys SET response=NULL,terminal_status='redacted',redacted_at=now()
  WHERE user_id=:user_id RETURNING 1
), refs AS (
  UPDATE chat_response_references SET
    state='unavailable',diary_id=NULL,rendered_metadata='{}'::jsonb,
    redacted_at=now(),redaction_reason='subject_deleting'
  WHERE user_id=:user_id RETURNING 1
), queued AS (
  UPDATE async_jobs SET
    state=CASE WHEN state='ready' THEN 'cancelled' ELSE state END,
    payload='{}'::jsonb,result_detail=NULL,payload_redacted_at=now(),
    finished_at=CASE WHEN state='ready' THEN now() ELSE finished_at END,
    result_code=CASE WHEN state='ready' THEN 'subject_deleting' ELSE result_code END
  WHERE user_id=:user_id AND state IN ('ready','running','succeeded','dead','cancelled')
  RETURNING 1
)
SELECT (SELECT count(*) FROM idem),(SELECT count(*) FROM refs),(SELECT count(*) FROM queued)
""")

_LEDGER = text("""
INSERT INTO privacy_ledger_events(operation_id,user_id,event,high_watermark)
VALUES (:operation_id,:user_id,:event,:high_watermark)
""")

_FINISH = text("""
UPDATE privacy_subject_barriers SET state='deleted',updated_at=now()
WHERE user_id=:user_id AND operation_id=:operation_id AND state='deleting'
RETURNING high_watermark
""")

_LOAD_BARRIER = text(
    "SELECT state, epoch, operation_id FROM privacy_subject_barriers WHERE user_id=:user_id"
)

# ─────────────────────────────────────────────────────────────
# 삭제 장벽 상태 — status(active|deleting|deleted) + epoch.
#
# ⚠️ 예전 계약은 "행이 있으면 차단"이었다. 이제 `active` 행이 존재하므로 **state를 봐야 한다.**
#    행 존재만으로 막으면 backfill 직후 전 사용자가 차단된다.
#
# 전환 모드(15장 2번):
#   compat   — 행이 없으면 허용(backfill 진행 중). 기본값.
#   enforced — 행이 없으면 거부(fail-closed). backfill 검증 두 sweep 통과 뒤 전환.
# ─────────────────────────────────────────────────────────────
STATUS_ACTIVE = "active"
STATUS_DELETING = "deleting"
STATUS_DELETED = "deleted"
STATUS_MISSING = "missing"  # 행 없음(런타임 판정값이며 DB 값이 아니다)

MODE_COMPAT = "compat"
MODE_ENFORCED = "enforced"

# `deleting` 동안 유일하게 허용되는 job type. 이게 없으면 삭제 자체가 진행되지 않는다.
#
# ⚠️ **실제로 등록된 처리기 이름이 반드시 들어 있어야 한다.** 한동안 `privacy_cleanup`이
# 빠져 있어서, 삭제를 시작하면 정리 잡이 만들어지자마자 소비자 게이트에 걸려 취소됐다.
# 잡은 생기는데 벡터는 영원히 안 지워지고, 오류도 안 나서 눈에 띄지 않았다.
# 아래 세 이름은 잡을 셋으로 나누려던 설계에서 온 것이고 아직 그런 처리기는 없다.
PRIVACY_JOB_ALLOWLIST: frozenset[str] = frozenset(
    {
        "privacy_cleanup",  # worker/privacy_jobs.py — 지금 실제로 도는 정리 잡
        "privacy_delete_coordinator",
        "privacy_provider_cleanup",
        "privacy_verify_residual",
    }
)


@dataclass(frozen=True, slots=True)
class BarrierState:
    status: str
    epoch: int
    operation_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class Authorization:
    allowed: bool
    reason: str  # 거부 사유는 잡 확정 코드로 그대로 쓰인다(계약)


async def load_barrier(session: AsyncSession, user_id: uuid.UUID) -> BarrierState:
    row = (await session.execute(_LOAD_BARRIER, {"user_id": user_id})).first()
    if row is None:
        return BarrierState(status=STATUS_MISSING, epoch=0, operation_id=None)
    return BarrierState(status=row[0], epoch=int(row[1] or 0), operation_id=row[2])


def authorize_job(
    barrier: BarrierState,
    *,
    job_type: str,
    payload_epoch: int | None = None,
    operation_id: uuid.UUID | None = None,
    mode: str = MODE_COMPAT,
) -> Authorization:
    """claim 직후와 외부 호출 전후에 적용하는 상태표(12.3절).

    | barrier    | 허용                                                              |
    |------------|-------------------------------------------------------------------|
    | `active`   | 같은 epoch의 일반 job. privacy coordinator는 불가                  |
    | `deleting` | 같은 epoch·operation_id의 allowlist privacy job만                  |
    | `deleted`  | 전부 거부                                                          |
    """
    is_privacy_job = job_type in PRIVACY_JOB_ALLOWLIST

    if barrier.status == STATUS_MISSING:
        # backfill 전이라 행이 없을 수 있다. enforced에서는 추정하지 않고 막는다.
        if mode == MODE_ENFORCED:
            return Authorization(False, "barrier_missing")
        return Authorization(not is_privacy_job, "ok" if not is_privacy_job else "barrier_missing")

    if barrier.status == STATUS_DELETED:
        return Authorization(False, "subject_deleted")

    if barrier.status == STATUS_DELETING:
        if not is_privacy_job:
            return Authorization(False, "subject_deleting")
        if payload_epoch is not None and payload_epoch != barrier.epoch:
            return Authorization(False, "epoch_mismatch")
        if operation_id is not None and operation_id != barrier.operation_id:
            return Authorization(False, "operation_mismatch")
        return Authorization(True, "ok")

    # active — 일반 job만. 삭제 coordinator가 여기서 돌면 살아 있는 계정을 지운다.
    if is_privacy_job:
        return Authorization(False, "coordinator_on_active")
    if payload_epoch is not None and payload_epoch != barrier.epoch:
        # 삭제 사이클을 지나온 옛 세대 잡 — 새 epoch에서 되살리지 않는다.
        return Authorization(False, "epoch_mismatch")
    return Authorization(True, "ok")


async def ensure_subject_active(session: AsyncSession, user_id: uuid.UUID) -> None:
    """대화 등 사용자 요청 진입점 게이트. `active`와 행 없음은 통과, 삭제 중/완료는 409."""
    barrier = await load_barrier(session, user_id)
    if barrier.status in (STATUS_DELETING, STATUS_DELETED):
        raise errors.AppError(
            "ACCOUNT_DELETING", 409, "계정 삭제를 처리하고 있어요."
        )


async def begin_subject_deletion(
    session: AsyncSession, *, user_id: uuid.UUID, operation_id: uuid.UUID
) -> tuple[int, int, int]:
    watermark = await session.scalar(
        _BEGIN, {"user_id": user_id, "operation_id": operation_id}
    )
    if watermark is None:
        # chat_context가 없는 가입 직후 계정도 장벽이 필요하다.
        await session.execute(
            text("""
            INSERT INTO privacy_subject_barriers(user_id,state,operation_id,high_watermark)
            VALUES (:user_id,'deleting',:operation_id,0)
            ON CONFLICT (user_id) DO UPDATE SET state='deleting',operation_id=:operation_id,
              epoch=privacy_subject_barriers.epoch + 1,updated_at=now()
            """),
            {"user_id": user_id, "operation_id": operation_id},
        )
        watermark = 0
    row = (await session.execute(_REDACT, {"user_id": user_id})).first()
    await session.execute(
        _LEDGER,
        {
            "operation_id": operation_id,
            "user_id": user_id,
            "event": "serving_blocked_and_redacted",
            "high_watermark": watermark,
        },
    )
    # 장벽만 세우고 끝내면 벡터가 그대로 남는다 — 실제로 지우는 잡을 여기서 건다.
    # 같은 트랜잭션이라 장벽 없이 삭제 잡만 도는 일이 없다.
    # ⚠️ 반드시 return **앞**이다. 뒤에 두면 실행되지 않아 벡터가 영원히 남는다(과거 결함).
    from app.services import jobs

    await jobs.enqueue(
        session,
        queue=jobs.QUEUE_MAINTENANCE,
        job_type="privacy_cleanup",
        user_id=user_id,
        dedup_key=f"privclean:{user_id}:{operation_id}:0",
        payload={"empty_sweeps": 0},
    )
    return tuple(int(value) for value in row) if row is not None else (0, 0, 0)



async def mark_subject_deleted(
    session: AsyncSession, *, user_id: uuid.UUID, operation_id: uuid.UUID
) -> bool:
    watermark = await session.scalar(
        _FINISH, {"user_id": user_id, "operation_id": operation_id}
    )
    if watermark is None:
        return False
    await session.execute(
        _LEDGER,
        {
            "operation_id": operation_id,
            "user_id": user_id,
            "event": "subject_deleted",
            "high_watermark": watermark,
        },
    )
    return True
