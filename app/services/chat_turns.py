"""채팅 추론 lease와 Phase-B publish CAS.

한 사용자에게 동시에 하나의 외부 추론만 허용한다. lease는 DB 트랜잭션보다 길게 살지만 만료가
있어 프로세스가 죽어도 영구 봉쇄되지 않는다. 최종 저장은 lease token과 context revision을 다시
대조한 뒤에만 허용한다.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors


@dataclass(frozen=True)
class Lease:
    token: uuid.UUID
    turn_seq: int
    base_context_revision: int


def diary_reference_capable(capabilities: str | None) -> bool:
    """응답 형태를 바꾸는 capability만 exact token으로 해석한다."""
    return "diary-reference-v1" in {
        token.strip() for token in (capabilities or "").split(",") if token.strip()
    }


def request_hash(
    *,
    text_value: str,
    greeting_id: str | None,
    diary_references: bool = False,
    context_ref: dict | None = None,
) -> str:
    wire = json.dumps(
        {
            "text": text_value,
            "greeting_id": greeting_id,
            "diary_references": diary_references,
            "context_ref": context_ref,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


_ENSURE_CONTEXT = text("""
INSERT INTO chat_contexts(user_id) VALUES (:user_id)
ON CONFLICT (user_id) DO NOTHING
""")

_ACQUIRE = text("""
INSERT INTO chat_active_turns
  (user_id,turn_seq,idempotency_key,request_hash,base_context_revision,lease_token,lease_until)
SELECT c.user_id,c.last_committed_turn_seq+1,:key,:request_hash,c.context_revision,:token,:lease_until
FROM chat_contexts c WHERE c.user_id=:user_id
ON CONFLICT (user_id) DO UPDATE SET
  turn_seq=EXCLUDED.turn_seq,
  idempotency_key=EXCLUDED.idempotency_key,
  request_hash=EXCLUDED.request_hash,
  base_context_revision=EXCLUDED.base_context_revision,
  lease_token=EXCLUDED.lease_token,
  lease_until=EXCLUDED.lease_until,
  created_at=now()
WHERE chat_active_turns.lease_until < now()
RETURNING lease_token,turn_seq,base_context_revision
""")

_VERIFY = text("""
SELECT a.turn_seq,a.base_context_revision
FROM chat_active_turns a
JOIN chat_contexts c ON c.user_id=a.user_id
WHERE a.user_id=:user_id AND a.lease_token=:token AND a.lease_until>=now()
  AND c.context_revision=a.base_context_revision
FOR UPDATE OF a
""")

_PUBLISH = text("""
UPDATE chat_contexts SET
  context_revision=context_revision+1,
  last_committed_turn_seq=:turn_seq,
  last_active_at=:now,
  updated_at=now()
WHERE user_id=:user_id AND context_revision=:base_revision
RETURNING context_revision
""")

_RELEASE = text(
    "DELETE FROM chat_active_turns WHERE user_id=:user_id AND lease_token=:token"
)


async def acquire(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    idempotency_key: str,
    request_digest: str,
    lease_seconds: float,
) -> Lease:
    await session.execute(_ENSURE_CONTEXT, {"user_id": user_id})
    token = uuid.uuid4()
    lease_until = datetime.now(timezone.utc) + timedelta(seconds=max(10.0, lease_seconds))
    row = (
        await session.execute(
            _ACQUIRE,
            {
                "user_id": user_id,
                "key": idempotency_key,
                "request_hash": request_digest,
                "token": token,
                "lease_until": lease_until,
            },
        )
    ).first()
    if row is None:
        raise errors.AppError(
            "CHAT_TURN_IN_PROGRESS",
            409,
            "앞선 답변을 마무리하고 있어요. 잠시 후 같은 키로 다시 시도해 주세요.",
            {"retry_after_seconds": 2},
        )
    return Lease(token=row[0], turn_seq=int(row[1]), base_context_revision=int(row[2]))


async def verify_publish(session: AsyncSession, *, user_id: uuid.UUID, lease: Lease) -> None:
    row = (
        await session.execute(_VERIFY, {"user_id": user_id, "token": lease.token})
    ).first()
    if row is None:
        raise errors.AppError(
            "CHAT_TURN_STALE", 409, "답변 순서가 바뀌었어요. 같은 요청을 다시 보내 주세요."
        )


async def finish_publish(
    session: AsyncSession, *, user_id: uuid.UUID, lease: Lease, now: datetime
) -> int:
    # last_active_at은 반드시 턴의 :now — now()로 바꾸면 저녁 푸시의 활동일 판정이 어긋난다.
    revision = (
        await session.execute(
            _PUBLISH,
            {
                "user_id": user_id,
                "turn_seq": lease.turn_seq,
                "base_revision": lease.base_context_revision,
                "now": now,
            },
        )
    ).scalar()
    if revision is None:
        raise errors.AppError(
            "CHAT_TURN_STALE", 409, "답변 순서가 바뀌었어요. 같은 요청을 다시 보내 주세요."
        )
    await session.execute(_RELEASE, {"user_id": user_id, "token": lease.token})
    return int(revision)


async def release(session: AsyncSession, *, user_id: uuid.UUID, lease: Lease) -> None:
    await session.execute(_RELEASE, {"user_id": user_id, "token": lease.token})
