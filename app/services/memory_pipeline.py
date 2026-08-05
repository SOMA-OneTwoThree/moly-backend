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


# 다음 처리 대상 = source table의 `MIN(turn_seq) > cursor`. 숫자 +1을 가정하지 않는다(불변식 3).
_NEXT_INGEST = text("""
SELECT MIN(turn_seq) FROM memory_source_turns
WHERE user_id=:user_id AND turn_seq > :cursor
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
