"""기억 v2 파이프라인 상태 — shadow 진입과 source 커서 전진.

기억 재설계 5단계(docs/capi-memory-ARCHITECTURE.md 15장 5번, 13.3절).

불변식:
 1. **shadow 진입은 한 transaction**이다. historical upper turn_seq를 먼저 고정하고
    `bootstrap_status=collecting`으로 바꾼다. 이 둘이 갈라지면 backfill 범위가 흔들린다.
 2. **bootstrap 완료 전에는 live turn을 먼저 처리하지 않는다.** collecting 동안 chat은 source를
    기록만 하고 mem0 consumer는 그 turn을 집어가지 않는다. 안 그러면 최신 turn이 과거보다 먼저
    색인돼 cursor 연속성이 깨진다.
 3. cursor는 숫자 `+1`을 가정하지 않는다. 다음 turn은 source table의 `MIN(turn_seq) > cursor`다.
 4. shadow에서 v2 결과를 **응답에 쓰지 않는다.** legacy read/write는 그대로 간다.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory_v2 import (  # noqa: F401  (상태 상수의 단일 소스)
    BOOTSTRAP_COLLECTING,
    BOOTSTRAP_LEGACY,
    BOOTSTRAP_READY,
    MODE_LEGACY,
    MODE_SHADOW,
    MODE_V2,
)
from app.services import relationship as rel

_log = logging.getLogger("moly-backend")


@dataclass(frozen=True, slots=True)
class PipelineState:
    user_id: uuid.UUID
    mode: str
    bootstrap_status: str
    source_through_turn_seq: int
    ingest_through_turn_seq: int
    consolidated_through_turn_seq: int
    historical_upper_turn_seq: int | None
    privacy_epoch: int
    revision: int

    @property
    def records_v2(self) -> bool:
        """v2 source를 기록해야 하는가. legacy 모드면 아무것도 쓰지 않는다."""
        return self.mode in (MODE_SHADOW, MODE_V2)

    @property
    def serves_v2(self) -> bool:
        """응답에 v2를 쓰는가. shadow는 기록만 하고 쓰지 않는다(불변식 4)."""
        return self.mode == MODE_V2

    @property
    def accepts_live_ingest(self) -> bool:
        """mem0 consumer가 live turn을 집어가도 되는가(불변식 2)."""
        return self.bootstrap_status == BOOTSTRAP_READY


_LOAD = text("""
SELECT user_id, mode, bootstrap_status, source_through_turn_seq, ingest_through_turn_seq,
       consolidated_through_turn_seq, historical_upper_turn_seq, privacy_epoch, revision
FROM memory_pipeline_states WHERE user_id=:user_id
""")


async def load(session: AsyncSession, user_id: uuid.UUID) -> PipelineState:
    """행이 없으면 legacy로 해석한다 — v2 도입 전 사용자는 아무것도 안 한다."""
    row = (await session.execute(_LOAD, {"user_id": user_id})).first()
    if row is None:
        return PipelineState(
            user_id=user_id,
            mode=MODE_LEGACY,
            bootstrap_status=BOOTSTRAP_LEGACY,
            source_through_turn_seq=0,
            ingest_through_turn_seq=0,
            consolidated_through_turn_seq=0,
            historical_upper_turn_seq=None,
            privacy_epoch=0,
            revision=0,
        )
    return PipelineState(
        user_id=row[0],
        mode=row[1],
        bootstrap_status=row[2],
        source_through_turn_seq=int(row[3]),
        ingest_through_turn_seq=int(row[4]),
        consolidated_through_turn_seq=int(row[5]),
        historical_upper_turn_seq=int(row[6]) if row[6] is not None else None,
        privacy_epoch=int(row[7]),
        revision=int(row[8]),
    )


# historical upper와 collecting을 **같은 문장**에서 고정한다(불변식 1).
# 이미 shadow/v2인 사용자는 건드리지 않는다 — 재진입이 범위를 다시 흔들면 안 된다.
_ENTER_SHADOW = text("""
INSERT INTO memory_pipeline_states
  (user_id, mode, bootstrap_status, historical_upper_turn_seq, privacy_epoch)
VALUES (:user_id, 'shadow', 'collecting', :upper, :privacy_epoch)
ON CONFLICT (user_id) DO UPDATE SET
  mode='shadow',
  bootstrap_status='collecting',
  historical_upper_turn_seq=COALESCE(
    memory_pipeline_states.historical_upper_turn_seq, EXCLUDED.historical_upper_turn_seq),
  revision=memory_pipeline_states.revision + 1,
  updated_at=now()
WHERE memory_pipeline_states.mode = 'legacy'
RETURNING historical_upper_turn_seq
""")

_MAX_TURN = text("""
SELECT COALESCE(MAX(turn_seq), 0) FROM messages WHERE user_id=:user_id AND kind='normal'
""")


async def enter_shadow(
    session: AsyncSession, user_id: uuid.UUID, *, privacy_epoch: int = 0
) -> int | None:
    """shadow 진입. 반환 = 고정된 historical upper. 이미 shadow/v2면 None(무변경).

    커밋은 호출측 소유 — 호출측이 같은 transaction에서 다른 작업과 묶을 수 있다.
    """
    upper = int(await session.scalar(_MAX_TURN, {"user_id": user_id}) or 0)
    row = (
        await session.execute(
            _ENTER_SHADOW,
            {"user_id": user_id, "upper": upper, "privacy_epoch": privacy_epoch},
        )
    ).first()
    if row is None:
        return None
    return int(row[0]) if row[0] is not None else upper


# 성공 chat Phase B가 source row와 **같은 transaction**에서 전진시킨다(13.3절).
# 뒤로 가지 않는다(GREATEST) — 재시도·순서 역전에도 커서가 되감기지 않는다.
_ADVANCE_SOURCE = text("""
UPDATE memory_pipeline_states
SET source_through_turn_seq = GREATEST(source_through_turn_seq, :turn_seq),
    updated_at = now()
WHERE user_id=:user_id AND mode <> 'legacy'
RETURNING source_through_turn_seq
""")


async def advance_source(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int
) -> int | None:
    """source 커서 전진. legacy 사용자면 None(아무것도 안 함)."""
    row = (
        await session.execute(_ADVANCE_SOURCE, {"user_id": user_id, "turn_seq": turn_seq})
    ).first()
    return int(row[0]) if row is not None else None


# 다음 처리 대상 = `MIN(turn_seq) > cursor`. 숫자 +1을 가정하지 않는다(불변식 3).
#
# ⚠️ source 좌표는 `messages.turn_seq`다. legacy `memory_source_turns`는 watermark 기반이라
#    turn_seq 컬럼이 없다 — 거기서 조회하면 런타임에 깨진다(13.3절: 새 좌표는 (user_id, turn_seq)).
#    historical upper를 넘어선 turn은 shadow 진입 시 고정한 범위 밖이므로 제외한다.
_NEXT_INGEST = text("""
SELECT MIN(m.turn_seq)
FROM messages m
JOIN memory_pipeline_states s ON s.user_id = m.user_id
WHERE m.user_id=:user_id
  AND m.kind='normal'
  AND m.turn_seq IS NOT NULL
  AND m.turn_seq > :cursor
  AND m.turn_seq <= s.source_through_turn_seq
""")


async def next_ingest_turn(
    session: AsyncSession, user_id: uuid.UUID, *, cursor: int
) -> int | None:
    """cursor 다음의 실제 source turn. 없으면 None(따라잡음)."""
    value = await session.scalar(_NEXT_INGEST, {"user_id": user_id, "cursor": cursor})
    return int(value) if value is not None else None


_MARK_READY = text("""
UPDATE memory_pipeline_states
SET bootstrap_status='ready', revision=revision + 1, updated_at=now()
WHERE user_id=:user_id AND bootstrap_status='collecting'
RETURNING historical_upper_turn_seq
""")


async def mark_bootstrap_ready(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """historical manifest 검증을 통과한 transaction만 호출한다(15장 7번).

    collecting이 아니면 아무것도 하지 않는다 — 두 번 열리면 live turn이 앞질러 들어간다.
    """
    row = (await session.execute(_MARK_READY, {"user_id": user_id})).first()
    return row is not None


# ─────────────────────────────────────────────────────────────
# 관계 event — 성공 turn과 같은 transaction에서 append. dedup_key가 중복 집계를 막는다.
# ─────────────────────────────────────────────────────────────
_ADD_EVENT = text("""
INSERT INTO relationship_events
  (user_id, event_type, turn_seq, activity_date, occurred_at, dedup_key)
VALUES (:user_id, :event_type, :turn_seq, :activity_date, :occurred_at, :dedup_key)
ON CONFLICT (user_id, dedup_key) DO NOTHING
RETURNING id
""")


async def record_turn_events(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    turn_seq: int,
    activity_date: date,
    occurred_at: datetime,
) -> int:
    """성공 normal turn 1건의 관계 event를 기록한다. 반환 = 실제로 삽입된 행 수.

    하루의 첫 turn이면 `active_day_started`도 함께 남긴다. 둘 다 dedup_key가 있어 재시도에 안전하다.
    """
    inserted = 0
    for event_type, dedup in (
        (rel.EVENT_NORMAL_TURN, rel.turn_dedup_key(turn_seq)),
        (rel.EVENT_ACTIVE_DAY, rel.active_day_dedup_key(activity_date)),
    ):
        row = (
            await session.execute(
                _ADD_EVENT,
                {
                    "user_id": user_id,
                    "event_type": event_type,
                    "turn_seq": turn_seq,
                    "activity_date": activity_date,
                    "occurred_at": occurred_at,
                    "dedup_key": dedup,
                },
            )
        ).first()
        if row is not None:
            inserted += 1
    return inserted


# ingest 커서 전진 — 이 turn의 모든 provider id가 registry에 기록된 뒤에만 부른다.
# 되감기지 않는다(GREATEST). fenced finalize와 같은 transaction에서 실행된다.
_ADVANCE_INGEST = text("""
UPDATE memory_pipeline_states
SET ingest_through_turn_seq = GREATEST(ingest_through_turn_seq, :turn_seq),
    updated_at = now()
WHERE user_id=:user_id
  AND mode <> 'legacy'
  -- source보다 앞설 수 없다. 앞서면 아직 안 만들어진 turn을 처리했다는 뜻이다.
  AND :turn_seq <= source_through_turn_seq
RETURNING ingest_through_turn_seq
""")


async def advance_ingest_cursor(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int
) -> int | None:
    """ingest 커서 전진. source를 앞지르면 아무것도 하지 않는다(None)."""
    row = (
        await session.execute(_ADVANCE_INGEST, {"user_id": user_id, "turn_seq": turn_seq})
    ).first()
    return int(row[0]) if row is not None else None


_ADVANCE_CONSOLIDATED = text("""
UPDATE memory_pipeline_states
SET consolidated_through_turn_seq = GREATEST(consolidated_through_turn_seq, :turn_seq),
    updated_at = now()
WHERE user_id=:user_id
  AND mode <> 'legacy'
  -- ingest를 앞지를 수 없다. 판정은 색인된 것에 대해서만 한다.
  AND :turn_seq <= ingest_through_turn_seq
RETURNING consolidated_through_turn_seq
""")


async def advance_consolidated_cursor(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int
) -> int | None:
    """consolidation 커서 전진. ingest를 앞지르면 아무것도 하지 않는다."""
    row = (
        await session.execute(_ADVANCE_CONSOLIDATED, {"user_id": user_id, "turn_seq": turn_seq})
    ).first()
    return int(row[0]) if row is not None else None


# ─────────────────────────────────────────────────────────────
# 잡 enqueue — 사용자별로 **한 번에 한 turn만** 흐르게 한다(13.3절).
#
# 여러 same-user 잡을 미리 만들어 두면 advisory lock에서 줄서기만 하고, 순서가 뒤집힐 여지도
# 생긴다. 최초 turn만 만들고 성공 finalize가 다음 turn을 만든다.
# ─────────────────────────────────────────────────────────────
JOB_MEM0_INGEST = "mem0_ingest"
JOB_MEM0_CONSOLIDATE = "mem0_consolidate"


def ingest_dedup_key(user_id: uuid.UUID, turn_seq: int, *, schema_version: str = "v1") -> str:
    """`(job_type, dedup_key)`가 멱등 키다. 같은 turn을 두 번 enqueue해도 한 행이다."""
    return f"mem0:{user_id}:{turn_seq}:{schema_version}"


def consolidate_dedup_key(
    user_id: uuid.UUID, turn_seq: int, *, schema_version: str = "v1", generation: int = 0
) -> str:
    """generation을 포함한다 — 같은 turn을 재처리하면 판정도 다시 돌아야 한다.

    generation 없이 고정 키를 쓰면 terminal 잡이 재enqueue를 영구히 막아, 재처리로 생긴
    pending registry가 영원히 판정되지 않는다(soak 실측).
    """
    return f"mem0c:{user_id}:{turn_seq}:{schema_version}:{generation}"


def provider_delete_dedup_key(user_id: uuid.UUID, turn_seq: int, *, generation: int = 0) -> str:
    return f"mem0d:{user_id}:{turn_seq}:{generation}"


async def enqueue_ingest(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int, privacy_epoch: int = 0
) -> uuid.UUID | None:
    """이 turn의 ingest 잡. 이미 있으면 None(멱등)."""
    from app.services import jobs

    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=JOB_MEM0_INGEST,
        user_id=user_id,
        dedup_key=ingest_dedup_key(user_id, turn_seq),
        payload={"turn_seq": turn_seq, "privacy_epoch": privacy_epoch},
    )


async def enqueue_consolidate(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int, privacy_epoch: int = 0
) -> uuid.UUID | None:
    from app.services import jobs

    generation = await _repair_generation(session, user_id)
    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=JOB_MEM0_CONSOLIDATE,
        user_id=user_id,
        dedup_key=consolidate_dedup_key(user_id, turn_seq, generation=generation),
        payload={"turn_seq": turn_seq, "privacy_epoch": privacy_epoch},
    )


JOB_MEM0_PROVIDER_DELETE = "mem0_provider_delete"

_REPAIR_GENERATION = text(
    "SELECT repair_generation FROM memory_pipeline_states WHERE user_id=:user_id"
)


async def _repair_generation(session: AsyncSession, user_id: uuid.UUID) -> int:
    return int(await session.scalar(_REPAIR_GENERATION, {"user_id": user_id}) or 0)


async def enqueue_provider_delete(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int, privacy_epoch: int = 0
) -> uuid.UUID | None:
    """non-active 기억의 provider 벡터 정리. 노출은 이미 semantic 필터가 막으므로 저장 비용용이다."""
    from app.services import jobs

    generation = await _repair_generation(session, user_id)
    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_MAINTENANCE,
        job_type=JOB_MEM0_PROVIDER_DELETE,
        user_id=user_id,
        dedup_key=provider_delete_dedup_key(user_id, turn_seq, generation=generation),
        payload={"turn_seq": turn_seq, "privacy_epoch": privacy_epoch, "limit": 50},
    )


JOB_SHADOW_TRACE = "shadow_prompt_trace"
JOB_SHADOW_CHECKPOINT = "shadow_checkpoint"
JOB_CONTRACT_COMPILE = "contract_compile"
JOB_RELATIONSHIP_PROJECT = "relationship_project"

# 이 turn과 직전 turn의 활동일. 다르면 하루가 닫힌 것이다.
_DAY_BOUNDARY = text("""
SELECT
  (SELECT activity_date FROM messages
   WHERE user_id=:user_id AND turn_seq=:turn_seq AND activity_date IS NOT NULL
   ORDER BY id LIMIT 1) AS today,
  (SELECT activity_date FROM messages
   WHERE user_id=:user_id AND turn_seq < :turn_seq AND activity_date IS NOT NULL
   ORDER BY turn_seq DESC, id DESC LIMIT 1) AS prev
""")


async def enqueue_shadow_checkpoints_on_day_boundary(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int, privacy_epoch: int = 0
) -> str | None:
    """하루가 닫혔으면 **직전 활동일**의 shadow checkpoint를 건다(15장 8번).

    매 turn 걸면 하루에 수십 번 LLM을 부른다. digest는 "activity date 하나의 독립 요약"이라
    하루가 닫힌 시점에 한 번이면 충분하다. 반환 = 대상 활동일(없으면 None).

    dedup key에 활동일이 들어가므로 같은 날을 두 번 만들지 않는다.
    """
    from app.services import jobs

    row = (await session.execute(
        _DAY_BOUNDARY, {"user_id": user_id, "turn_seq": turn_seq}
    )).first()
    if row is None or row[0] is None or row[1] is None or row[0] == row[1]:
        return None  # 첫 turn이거나 같은 날 — 아직 닫히지 않았다
    closed = row[1]

    await jobs.enqueue(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=JOB_SHADOW_CHECKPOINT,
        user_id=user_id,
        dedup_key=f"sckpt:d:{user_id}:{closed.isoformat()}",
        payload={"kind": "daily_digest", "activity_date": closed.isoformat(),
                 "privacy_epoch": privacy_epoch},
    )
    # 관계 상태 투영도 하루 경계다 — active_days가 날짜 단위라 매 턴 돌릴 이유가 없다.
    await jobs.enqueue(
        session,
        queue=jobs.QUEUE_MAINTENANCE,
        job_type=JOB_RELATIONSHIP_PROJECT,
        user_id=user_id,
        dedup_key=f"relproj:{user_id}:{closed.isoformat()}",
        payload={"privacy_epoch": privacy_epoch},
    )
    # 계약 추출도 하루 경계에서 한다. 매 턴 돌리면 같은 합의를 반복해서 뽑고 비용만 든다.
    await jobs.enqueue(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=JOB_CONTRACT_COMPILE,
        user_id=user_id,
        dedup_key=f"contract:{user_id}:{closed.isoformat()}",
        payload={"after_message_id": 0, "privacy_epoch": privacy_epoch},
    )
    # 누적 window는 그날까지의 대화를 이어 붙인다. digest와 달리 체인이라 하루에 한 고리다.
    await jobs.enqueue(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=JOB_SHADOW_CHECKPOINT,
        user_id=user_id,
        dedup_key=f"sckpt:w:{user_id}:{closed.isoformat()}",
        payload={"kind": "window", "privacy_epoch": privacy_epoch},
    )
    return closed.isoformat()


async def enqueue_shadow_trace(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int, privacy_epoch: int = 0
) -> uuid.UUID | None:
    """이 turn의 프롬프트 계측 잡(15장 9번). 실패해도 대화에는 영향이 없다.

    maintenance 큐다 — 계측이 content 큐를 막아 기억 색인을 늦추면 안 된다.
    """
    from app.services import jobs

    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_MAINTENANCE,
        job_type=JOB_SHADOW_TRACE,
        user_id=user_id,
        dedup_key=f"trace:{user_id}:{turn_seq}",
        payload={"turn_seq": turn_seq, "privacy_epoch": privacy_epoch},
    )


async def enqueue_next_ingest(
    session: AsyncSession, user_id: uuid.UUID, *, cursor: int, privacy_epoch: int = 0
) -> int | None:
    """cursor 다음 turn의 ingest 잡을 만든다. 없으면 None(따라잡음).

    성공 finalize가 이걸 부른다 — 한 사용자의 잡이 항상 한 개만 대기하게 하는 장치다.
    """
    nxt = await next_ingest_turn(session, user_id, cursor=cursor)
    if nxt is None:
        return None
    await enqueue_ingest(session, user_id, turn_seq=nxt, privacy_epoch=privacy_epoch)
    return nxt
