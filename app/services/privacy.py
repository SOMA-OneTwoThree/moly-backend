"""계정 삭제 오케스트레이터가 호출하는 moly-backend 측 삭제 장벽.

인증 계정 삭제 자체는 moly-auth 소유다. 그 오케스트레이터는 프로필을 지우기 전에
``begin_subject_deletion``을 같은 DB에 적용해야 한다. 이후 worker publish가 차단되고 응답 사본과
잡 payload가 즉시 비식별화된다. 외부 삭제 완료 후 ``mark_subject_deleted``로 감사 좌표를 닫는다.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors


_BEGIN = text("""
INSERT INTO privacy_subject_barriers(user_id,state,operation_id,high_watermark)
SELECT :user_id,'deleting',:operation_id,c.memory_source_watermark
FROM chat_contexts c WHERE c.user_id=:user_id
ON CONFLICT (user_id) DO UPDATE SET
  state='deleting',operation_id=EXCLUDED.operation_id,
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

_IS_BLOCKED = text(
    "SELECT EXISTS(SELECT 1 FROM privacy_subject_barriers WHERE user_id=:user_id)"
)


async def ensure_subject_active(session: AsyncSession, user_id: uuid.UUID) -> None:
    if await session.scalar(_IS_BLOCKED, {"user_id": user_id}):
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
            ON CONFLICT (user_id) DO UPDATE SET state='deleting',operation_id=:operation_id,updated_at=now()
            """),
            {"user_id": user_id, "operation_id": operation_id},
        )
        watermark = 0
    row = (await session.execute(_REDACT, {"user_id": user_id})).first()
    # 저녁 푸시 개인화 문구도 대화 파생 사본 — 장벽 설정 즉시 제거(프로필 CASCADE만 기다리면
    # begin~mark 사이 구간에 남는다). to_regclass 가드: 코드가 마이그레이션보다 먼저 배포된
    # 상태에서도 삭제 플로우가 깨지지 않게(테이블 생기면 자동 활성).
    if await session.scalar(
        text("SELECT to_regclass('public.push_personalizations') IS NOT NULL")
    ):
        await session.execute(
            text("DELETE FROM push_personalizations WHERE user_id=:user_id"),
            {"user_id": user_id},
        )
    await session.execute(
        _LEDGER,
        {
            "operation_id": operation_id,
            "user_id": user_id,
            "event": "serving_blocked_and_redacted",
            "high_watermark": watermark,
        },
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
