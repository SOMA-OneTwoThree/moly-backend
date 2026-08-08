"""기억 v2 파이프라인 상태 — shadow 진입과 source 커서 전진.

기억 재설계 5단계(docs/ARCHITECTURE-capi.md 5.1절).

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
from datetime import date, datetime, timedelta, timezone

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
    # 재처리 세대. 커서를 되돌려 같은 turn을 다시 처리할 때만 올라간다.
    # **잡 멱등 키와 후보 계획의 소유자를 가르는 값이다** — 세대가 다르면 남남이다.
    repair_generation: int = 0

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
       consolidated_through_turn_seq, historical_upper_turn_seq, privacy_epoch, revision,
       repair_generation
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
            repair_generation=0,
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
        repair_generation=int(row[9] or 0),
    )


# historical upper와 collecting을 **같은 문장**에서 고정한다(불변식 1).
# 이미 shadow/v2인 사용자는 건드리지 않는다 — 재진입이 범위를 다시 흔들면 안 된다.
#
# ⚠️ source 커서도 여기서 upper까지 올린다. 이 줄이 없으면 진입 직후 커서가 0이라
# `_NEXT_INGEST`의 `turn_seq <= source_through_turn_seq`가 아무 턴도 통과시키지 못하고,
# backfill이 시작조차 못 한다(dev 실측: 104턴 보유 사용자가 진입 직후 next=None).
# "source"는 **대화가 커밋되었다**는 뜻이고 과거 대화는 이미 커밋돼 있다 — 0은 사실과 다르다.
_ENTER_SHADOW = text("""
INSERT INTO memory_pipeline_states
  (user_id, mode, bootstrap_status, historical_upper_turn_seq, source_through_turn_seq,
   privacy_epoch)
VALUES (:user_id, 'shadow', 'collecting', :upper, :upper, :privacy_epoch)
ON CONFLICT (user_id) DO UPDATE SET
  mode='shadow',
  bootstrap_status='collecting',
  historical_upper_turn_seq=COALESCE(
    memory_pipeline_states.historical_upper_turn_seq, EXCLUDED.historical_upper_turn_seq),
  source_through_turn_seq=GREATEST(
    memory_pipeline_states.source_through_turn_seq, EXCLUDED.source_through_turn_seq),
  revision=memory_pipeline_states.revision + 1,
  updated_at=now()
WHERE memory_pipeline_states.mode = 'legacy'
RETURNING historical_upper_turn_seq
""")

# 신규 가입자 등록.
#
# ⚠️ 이게 없으면 **가입하는 사람마다 평생 기억이 0건**이다. `memory_pipeline_states` 행을
#    만드는 코드가 지금까지 스크립트(`enter_shadow_cohort.py`)에만 있었다. 가입·온보딩·첫 대화
#    어디에도 없어서, 행이 없는 사용자는 `load()`가 legacy로 해석하고 chat이 즉시 반환했다.
#    오류도 로그도 안 났다.
#
# 과거 대화가 없으므로 backfill 범위도 없다 — 바로 `ready`로 두어도 불변식 2가 깨지지 않는다.
# 이미 행이 있으면 건드리지 않는다(legacy로 둔 사용자를 되살리지 않는다).
_ENROLL = text("""
INSERT INTO memory_pipeline_states
  (user_id, mode, bootstrap_status, historical_upper_turn_seq,
   source_through_turn_seq, privacy_epoch)
VALUES (:user_id, 'v2', 'ready', 0, 0, 0)
ON CONFLICT (user_id) DO NOTHING
RETURNING user_id
""")


async def enroll(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """기억 파이프라인에 처음 등록한다. 이미 행이 있으면 False(아무것도 안 함).

    커밋은 호출측 소유 — 첫 turn과 같은 transaction에 묶는다.
    """
    row = (await session.execute(_ENROLL, {"user_id": user_id})).first()
    return row is not None


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
# ⚠️ **세대를 확인한다.** 재추출이 커서를 0으로 내린 뒤, 그 전에 떠 있던 잡이 뒤늦게 끝나면
#    `GREATEST`가 커서를 옛 위치까지 다시 밀어 올린다. 그 사이 구간은 재추출 대상에서 통째로
#    빠지고 아무도 눈치채지 못한다. 판정 결과 반영은 이미 revision을 확인하는데(registry_repo)
#    커서만 안 보고 있었다.
_ADVANCE_INGEST = text("""
UPDATE memory_pipeline_states
SET ingest_through_turn_seq = GREATEST(ingest_through_turn_seq, :turn_seq),
    updated_at = now()
WHERE user_id=:user_id
  AND mode <> 'legacy'
  AND repair_generation = :generation
  -- source보다 앞설 수 없다. 앞서면 아직 안 만들어진 turn을 처리했다는 뜻이다.
  AND :turn_seq <= source_through_turn_seq
RETURNING ingest_through_turn_seq
""")


async def advance_ingest_cursor(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int, generation: int = 0
) -> int | None:
    """ingest 커서 전진. 전진하지 못하면 None.

    None인 경우는 둘이다 — source를 앞지르려 했거나, 그 사이 세대가 바뀌어 이 잡의 결과가
    낡았거나. 둘 다 **커서를 안 옮기는 것이 맞다.** 그 사용자는 훑기(sweep)가 커서 기준으로
    다시 집어간다.
    """
    row = (
        await session.execute(
            _ADVANCE_INGEST,
            {"user_id": user_id, "turn_seq": turn_seq, "generation": generation},
        )
    ).first()
    if row is None:
        _log.warning(
            "ingest 커서가 전진하지 못했다 — user=%s turn=%s 세대=%s "
            "(세대가 바뀌었거나 source를 앞지르려 했다)",
            user_id, turn_seq, generation,
        )
        return None
    return int(row[0])


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


def ingest_dedup_key(
    user_id: uuid.UUID, cursor: int, *, schema_version: str = "v1", generation: int = 0
) -> str:
    """`(job_type, dedup_key)`가 멱등 키다.

    ⚠️ 키의 기준은 **이 잡이 출발할 커서**다. 처리 대상 턴이 아니다.

    예전에는 "요청한 턴 번호"로 키를 만들었는데, 실제 처리는 **대화 덩어리가 끊기는 데까지**만
    한다. 덩어리가 요청 턴보다 앞에서 끊기면(하루 경계 등) 커서는 그 앞에서 멈추고, 잡이
    끝나면서 "다음은 요청 턴" 하고 새 잡을 만들려다 **자기가 방금 쓴 키와 부딪힌다.**
    `ON CONFLICT DO NOTHING`이라 잡은 안 생기고 오류도 안 난다 — 그 사람의 기억이 조용히
    영원히 멈춘다(2026-08-08 운영에서 실제로 한 명이 그 상태였다).

    커서는 성공할 때마다 반드시 앞으로 가므로 같은 커서가 두 번 필요해지지 않는다. 재시도는
    같은 커서라 그대로 멱등이다.

    세대는 `memory_pipeline_states.repair_generation`을 쓴다. 판정 잡·삭제 잡과 **같은 값이어야
    한다** — 다르면 한쪽만 새 키를 받아, 추출은 다시 도는데 판정 잡은 옛 키에 막혀 조용히
    무시된다(그 기억은 `pending`인 채로 영원히 안 보인다).

    세대 0은 접미사를 안 붙인다. 0이 아니면 **`g`를 앞에 붙인다** — 잠깐 이 자리에 `revision`을
    쓴 적이 있어서 운영에 접미사 `2`·`3`·`5`인 키가 남아 있다(실제 측정). 숫자만 붙이면
    세대가 2로 올라갈 때 그 키와 같은 문자열이 된다.
    """
    base = f"mem0:{user_id}:c{cursor}:{schema_version}"
    return base if generation == 0 else f"{base}:g{generation}"


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
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    turn_seq: int,
    cursor: int,
    privacy_epoch: int = 0,
    delay_s: float = 0.0,
    generation: int | None = None,
) -> uuid.UUID | None:
    """이 turn의 ingest 잡. 이미 있으면 None(멱등).

    `delay_s`를 주면 그만큼 뒤에 실행된다. **대화가 잠잠해질 때까지 기다리는 장치다.**
    기다리는 동안 같은 사용자의 새 턴은 잡을 만들지 않는다(chat이 커서가 따라잡았을 때만
    걸기 때문). 그래서 잡 하나가 그동안 쌓인 구간을 통째로 먹는다 — 한 사건이 여러 턴에
    걸쳐도 조각나지 않는다.

    사람도 대화 중에 정리하지 않고 끝나고 나서 남긴다. 지연은 사용자에게 안 보인다 —
    최근 대화는 원문 그대로 프롬프트에 들어가므로 회상이 없어도 캐피가 방금 얘기를 안다.

    `generation`을 안 주면 그 사용자의 `repair_generation`을 직접 읽는다 — 호출측이 빠뜨려서
    판정 잡과 세대가 어긋나는 사고를 막는다.

    `cursor`는 **이 잡이 출발할 지점**이고 멱등 키의 기준이다. `turn_seq`는 payload에만 들어가
    덩어리 상한을 잡는 데 쓴다. 둘을 헷갈리면 키가 겹쳐 사슬이 조용히 끊긴다.
    """
    from app.services import jobs

    if generation is None:
        generation = await _repair_generation(session, user_id)
    available_at = (
        datetime.now(timezone.utc) + timedelta(seconds=delay_s) if delay_s > 0 else None
    )
    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_MEMORY,
        job_type=JOB_MEM0_INGEST,
        user_id=user_id,
        dedup_key=ingest_dedup_key(user_id, cursor, generation=generation),
        payload={"turn_seq": turn_seq, "privacy_epoch": privacy_epoch},
        available_at=available_at,
    )


async def enqueue_consolidate(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int, privacy_epoch: int = 0
) -> uuid.UUID | None:
    from app.services import jobs

    generation = await _repair_generation(session, user_id)
    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_MEMORY,
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


JOB_CONTRACT_COMPILE = "contract_compile"
JOB_RELATIONSHIP_PROJECT = "relationship_project"
JOB_RECONSOLIDATE = "mem0_reconsolidate"
JOB_MEMORY_SWEEP = "memory_gap_sweep"


async def enqueue_memory_sweep(session: AsyncSession, *, bucket: str) -> uuid.UUID | None:
    """멈춘 기억 파이프라인을 훑어 되살리는 잡. 틱이 부른다.

    ingest는 체인이라 잡 하나가 dead가 되면 그 사용자는 영영 멈춘다 — chat은
    `ingest >= source`일 때만 새 잡을 걸고, `ingest_dedup_key`가 (user, turn)으로 고정이라
    죽은 turn은 다시 걸리지도 않는다. 그래서 훑어서 replay하는 주체가 따로 필요하다.

    `bucket`이 dedup 단위다 — 같은 창에서는 한 번만 생긴다.
    """
    from app.services import jobs

    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_MAINTENANCE,
        job_type=JOB_MEMORY_SWEEP,
        dedup_key=f"memsweep:{bucket}",
        payload={},
    )

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


async def enqueue_day_boundary_jobs(
    session: AsyncSession, user_id: uuid.UUID, *, turn_seq: int, privacy_epoch: int = 0
) -> str | None:
    """하루가 닫혔으면 **직전 활동일**에 대한 뒷정리 잡을 건다. 반환 = 대상 활동일(없으면 None).

    매 turn 걸면 하루에 수십 번 LLM을 부르는 것들이라 하루가 닫힌 시점에 한 번만 건다.
    dedup key에 활동일이 들어가므로 같은 날을 두 번 만들지 않는다.
    """
    from app.services import jobs

    row = (await session.execute(
        _DAY_BOUNDARY, {"user_id": user_id, "turn_seq": turn_seq}
    )).first()
    if row is None or row[0] is None or row[1] is None or row[0] == row[1]:
        return None  # 첫 turn이거나 같은 날 — 아직 닫히지 않았다
    closed = row[1]

    # 살아 있는 기억끼리 재판정. 판정기 규칙이 나아져도 이미 active로 굳은 것은 그대로
    # 남기 때문에(실측: 같은 뜻 두 건이 둘 다 active), 하루에 한 번 잔여를 정리한다.
    await jobs.enqueue(
        session,
        queue=jobs.QUEUE_MEMORY,
        job_type=JOB_RECONSOLIDATE,
        user_id=user_id,
        dedup_key=f"recons:{user_id}:{closed.isoformat()}",
        payload={"privacy_epoch": privacy_epoch},
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
    return closed.isoformat()


async def enqueue_next_ingest(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    cursor: int,
    privacy_epoch: int = 0,
    generation: int | None = None,
) -> int | None:
    """cursor 다음 turn의 ingest 잡을 만든다. 없으면 None(따라잡음).

    성공 finalize가 이걸 부른다 — 한 사용자의 잡이 항상 한 개만 대기하게 하는 장치다.

    `generation`을 안 주면 `repair_generation`을 읽는다. 재추출로 커서를 되돌리면 이 값이
    올라가고, 그래야 **이미 처리한 turn을 다시 처리할 수 있다.**
    """
    nxt = await next_ingest_turn(session, user_id, cursor=cursor)
    if nxt is None:
        return None
    made = await enqueue_ingest(
        session, user_id, turn_seq=nxt, cursor=cursor,
        privacy_epoch=privacy_epoch, generation=generation,
    )
    if made is None:
        # **사슬의 유일한 연결 고리다.** 여기서 조용히 끊기면 그 사용자의 기억이 영원히 멈춘다.
        # 커서 기준 키라 정상적으로는 안 겹치지만, 겹쳤다면 이미 끝난 잡에 막힌 것이므로
        # 유일한 키로 하나 더 만든다. 같은 구간을 두 번 처리해도 id가 결정적이라 데이터는
        # 수렴한다 — 사슬이 끊기는 것보다 낫다.
        from app.services import jobs

        _log.warning(
            "다음 추출 잡이 키에 막혔다 — user=%s 커서=%s 다음턴=%s 세대=%s. 유일 키로 다시 건다",
            user_id, cursor, nxt, generation,
        )
        await jobs.enqueue(
            session,
            queue=jobs.QUEUE_MEMORY,
            job_type=JOB_MEM0_INGEST,
            user_id=user_id,
            dedup_key=f"{ingest_dedup_key(user_id, cursor, generation=generation or 0)}:r{nxt}",
            payload={"turn_seq": nxt, "privacy_epoch": privacy_epoch},
        )
    return nxt
