"""정규화 기억(W8)의 **저장소** — SQL과 트랜잭션 불변식. 판정은 `memory_reconcile`이 한다.

여기서 지키는 것(어기면 조용히 깨진다):

1. **cross-user evidence 차단.** `memory_evidence`의 FK는 user_id를 태우지 않으므로(messages PK=(id))
   DB 제약만으로는 남의 메시지를 근거로 다는 것을 못 막는다. insert 전에 **같은 트랜잭션에서**
   `messages.user_id = fact.user_id`를 확인하고, 하나라도 어긋나면 아무것도 쓰지 않는다.
2. **terminal 불가역.** fact를 다시 `active`로 만드는 UPDATE가 이 모듈에 **하나도 없다**.
   상태를 바꾸는 문장은 전부 `WHERE ... status='active'`이고, 0행이면 되살리지 않고 실패시킨다.
3. **revision은 실제 변경 때만 정확히 +1.** IGNORE만 있는 배치·재시도·no-op은 올리지 않는다.
   여기서 헛돌면 관계 프로필(W9)이 같은 입력으로 계속 재생성된다.
4. **watermark는 turn당 하나.** 대표 메시지는 그 turn의 inbound user message여야 하고, 없으면
   추출 소스로 만들지 않는다(예외로 알려 호출측이 enqueue를 건너뛰게 한다).
5. 자연어 컬럼은 저장 직전 `naming.to_placeholder`를 한 번 더 통과한다(자연 멱등).

커밋하지 않는다 — 전부 호출측 트랜잭션 경계에 합류한다(잡 finalize와 원자적으로 묶기 위함).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import jobs, memory, memory_norm
from app.services.memory_candidates import (
    ACTION_ADD,
    ACTION_IGNORE,
    ACTION_KEEP_BOTH,
    ACTION_REINFORCE,
    ACTION_SUPERSEDE,
)
from app.services.memory_reconcile import Closure, Decision, ExistingFact, ForgetMarker

# v1 추출 소스는 이것 하나뿐이다. 일기·루틴·프로필 이벤트는 각자의 watermark/closure 계약이
# 생기기 전까지 추가하지 않는다(DB CHECK와 같은 값).
SOURCE_KIND_CONVERSATION_TURN = "conversation_turn"

# 잡 계약 — W7 `async_jobs`의 content 큐 위에서 돈다.
JOB_MEMORY_EXTRACT = "memory_extract"
JOB_MEMORY_RECONCILE = "memory_reconcile"
JOB_PROFILE_REFRESH = "relationship_profile_refresh"
SOURCE_PAYLOAD_SCHEMA_VERSION = "memory-source-v1"


class MemoryRepoError(Exception):
    """저장소 불변식 위반 — 부분 반영 없이 트랜잭션을 실패시킨다."""


class CrossUserEvidenceError(MemoryRepoError):
    """다른 유저(또는 없는) 메시지를 근거로 달려는 시도. 절대 저장하지 않는다."""


class NoInboundUserMessageError(MemoryRepoError):
    """대표 inbound user message가 없는 turn — 추출 소스로 enqueue하지 않는다(선발화만 있는 turn 등)."""


class TerminalFactError(MemoryRepoError):
    """이미 forgotten/superseded인 fact를 바꾸려 했다. 되살리지 않고 실패시킨다."""


@dataclass(frozen=True, slots=True)
class SourceTurn:
    """배정된 turn 좌표 + 그 시점 유저의 기억 상태."""

    watermark: int
    memory_generation: int
    memory_mode: str
    message_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """reconcile 적용 결과. `revision`은 실제 변경이 있었을 때만 채워진다."""

    changed: bool
    revision: int | None
    added: int = 0
    reinforced: int = 0
    superseded: int = 0
    ignored: int = 0


# ─────────────────────────────────────────────────────────────
# 읽기
# ─────────────────────────────────────────────────────────────
_MODE_SQL = text("SELECT memory_mode FROM chat_contexts WHERE user_id=:user_id")

_MESSAGES_SQL = text(
    "SELECT id, sender, content FROM messages WHERE user_id=:user_id AND id IN :ids"
).bindparams(bindparam("ids", expanding=True))

# active fact + 근거 message id + 그 근거가 가리키는 최대 watermark(SUPERSEDE 최신성 비교의 기준).
_ACTIVE_FACTS_SQL = text("""
SELECT f.id, f.kind, f.subject, f.predicate, f.content_hash, f.normalization_version,
       f.confidence,
       COALESCE(array_agg(e.source_id) FILTER (WHERE e.source_id IS NOT NULL), '{}') AS evidence_ids,
       max(tm.source_watermark) AS max_watermark
FROM memory_facts f
LEFT JOIN memory_evidence e ON e.fact_id = f.id
LEFT JOIN memory_source_turn_messages tm
       ON tm.user_id = f.user_id AND tm.message_id = e.source_id
WHERE f.user_id = :user_id AND f.status = 'active'
GROUP BY f.id
""")

_MARKERS_SQL = text("""
SELECT scope, predicate, normalized_hash, normalization_version
FROM memory_forget_markers WHERE user_id = :user_id
""")

_CLOSURES_SQL = text("""
SELECT from_watermark, through_watermark FROM memory_source_closures
WHERE user_id = :user_id AND source_kind = :source_kind
  AND from_watermark <= :through_watermark AND through_watermark >= :from_watermark
""")

_WATERMARKS_SQL = text("""
SELECT message_id, source_watermark FROM memory_source_turn_messages
WHERE user_id = :user_id AND message_id IN :ids
""").bindparams(bindparam("ids", expanding=True))

_RANGE_MESSAGES_SQL = text("""
SELECT message_id FROM memory_source_turn_messages
WHERE user_id = :user_id AND source_watermark BETWEEN :from_watermark AND :through_watermark
""")


async def resolve_mode(session: AsyncSession, user_id: uuid.UUID | str) -> str:
    """유저의 기억 모드. 행이 없으면 legacy(= 아직 대화 컨텍스트가 없는 유저)."""
    row = (await session.execute(_MODE_SQL, {"user_id": user_id})).first()
    return row[0] if row is not None else memory.MODE_LEGACY


async def load_active_facts(
    session: AsyncSession, user_id: uuid.UUID | str
) -> list[ExistingFact]:
    rows = (await session.execute(_ACTIVE_FACTS_SQL, {"user_id": user_id})).mappings().all()
    return [
        ExistingFact(
            id=r["id"],
            kind=r["kind"],
            subject=r["subject"],
            predicate=r["predicate"],
            content_hash=r["content_hash"],
            normalization_version=r["normalization_version"],
            confidence=float(r["confidence"]),
            evidence_message_ids=frozenset(r["evidence_ids"] or ()),
            max_source_watermark=r["max_watermark"],
        )
        for r in rows
    ]


async def load_forget_markers(
    session: AsyncSession, user_id: uuid.UUID | str
) -> list[ForgetMarker]:
    """그 유저의 **전** marker. hard filter가 각 marker version의 normalizer로 재계산해야 하므로
    version별로 걸러 읽지 않는다(걸러 읽으면 지원 못 하는 version을 조용히 지나친다)."""
    rows = (await session.execute(_MARKERS_SQL, {"user_id": user_id})).mappings().all()
    return [
        ForgetMarker(
            scope=r["scope"],
            predicate=r["predicate"],
            normalized_hash=r["normalized_hash"],
            normalization_version=r["normalization_version"],
        )
        for r in rows
    ]


async def load_closures(
    session: AsyncSession,
    user_id: uuid.UUID | str,
    *,
    from_watermark: int,
    through_watermark: int,
) -> list[Closure]:
    rows = (
        await session.execute(
            _CLOSURES_SQL,
            {
                "user_id": user_id,
                "source_kind": SOURCE_KIND_CONVERSATION_TURN,
                "from_watermark": from_watermark,
                "through_watermark": through_watermark,
            },
        )
    ).mappings().all()
    return [Closure(from_watermark=r["from_watermark"], through_watermark=r["through_watermark"])
            for r in rows]


async def watermarks_for_messages(
    session: AsyncSession, user_id: uuid.UUID | str, message_ids: Sequence[int]
) -> dict[int, int]:
    """evidence message_id → source_watermark. 연결이 없는 id는 결과에서 빠진다(판정이 실패한다)."""
    if not message_ids:
        return {}
    rows = (
        await session.execute(
            _WATERMARKS_SQL, {"user_id": user_id, "ids": list(dict.fromkeys(message_ids))}
        )
    ).mappings().all()
    return {r["message_id"]: r["source_watermark"] for r in rows}


async def assert_payload_message_ids(
    session: AsyncSession,
    user_id: uuid.UUID | str,
    *,
    from_watermark: int,
    through_watermark: int,
    message_ids: Sequence[int],
) -> None:
    """coalesce payload의 `message_ids`가 구간에 연결된 메시지의 **정확한 합집합**인지 확인한다.

    누락·추가·타 유저 id가 하나라도 있으면 publish하지 않는다 — payload를 그대로 믿으면 닫힌 구간의
    메시지나 남의 메시지가 근거로 섞인다.
    """
    rows = (
        await session.execute(
            _RANGE_MESSAGES_SQL,
            {
                "user_id": user_id,
                "from_watermark": from_watermark,
                "through_watermark": through_watermark,
            },
        )
    ).all()
    expected = {r[0] for r in rows}
    got = set(message_ids)
    if expected != got:
        raise CrossUserEvidenceError(
            f"payload message_ids가 구간 {from_watermark}~{through_watermark}의 합집합과 다르다 "
            f"(누락={sorted(expected - got)}, 초과={sorted(got - expected)})"
        )


# ─────────────────────────────────────────────────────────────
# turn 배정 — Phase 2 커밋이 메시지와 **같은 트랜잭션**에서 쓴다.
# ─────────────────────────────────────────────────────────────
_ALLOC_SQL = text("""
INSERT INTO chat_contexts (user_id, memory_source_watermark)
VALUES (:user_id, 1)
ON CONFLICT (user_id) DO UPDATE
  SET memory_source_watermark = chat_contexts.memory_source_watermark + 1
RETURNING memory_source_watermark, memory_generation, memory_mode
""")

_INSERT_TURN_SQL = text("""
INSERT INTO memory_source_turns
  (user_id, source_watermark, representative_message_id, committed_at)
VALUES (:user_id, :source_watermark, :representative_message_id, :committed_at)
""")

_INSERT_TURN_MESSAGE_SQL = text("""
INSERT INTO memory_source_turn_messages (user_id, source_watermark, message_id)
VALUES (:user_id, :source_watermark, :message_id)
""")


async def _load_messages(
    session: AsyncSession, user_id: uuid.UUID | str, message_ids: Sequence[int]
) -> dict[int, tuple[str, str]]:
    """`{id: (sender, content)}`. **그 유저 것만** 조회하므로 누락 = 타 유저이거나 없는 메시지다."""
    unique = list(dict.fromkeys(message_ids))
    if not unique:
        raise CrossUserEvidenceError("message_ids가 비어 있다")
    rows = (await session.execute(_MESSAGES_SQL, {"user_id": user_id, "ids": unique})).mappings().all()
    found = {r["id"]: (r["sender"], r["content"]) for r in rows}
    missing = [i for i in unique if i not in found]
    if missing:
        raise CrossUserEvidenceError(f"해당 유저의 메시지가 아니거나 존재하지 않는다: {missing}")
    return found


async def load_turn_messages(
    session: AsyncSession, user_id: uuid.UUID | str, message_ids: Sequence[int]
) -> dict[int, tuple[str, str]]:
    """turn 메시지 `{id: (sender, content)}` 적재 — **cross-user 검증 포함**(불변식 1).

    추출 잡이 프롬프트를 만들 때 쓴다. 같은 검증을 잡 쪽에 다시 구현하면 방어선이 갈라지므로
    조회 경로를 여기 하나로 둔다.
    """
    return await _load_messages(session, user_id, message_ids)


async def allocate_source_turn(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    representative_message_id: int,
    message_ids: Sequence[int],
    committed_at: datetime,
) -> SourceTurn:
    """turn 하나에 watermark 하나를 배정하고 그 turn의 메시지를 연결한다(커밋하지 않는다).

    대표 메시지는 그 turn을 시작해 `post_message`를 발생시킨 **inbound user message**여야 한다.
    없으면 `NoInboundUserMessageError` — 호출측은 추출 잡을 만들지 않는다.
    """
    ids = list(dict.fromkeys(message_ids))
    if representative_message_id not in ids:
        ids.insert(0, representative_message_id)
    loaded = await _load_messages(session, user_id, ids)  # cross-user 차단(대표 포함)

    sender, _ = loaded[representative_message_id]
    if sender != "user":
        raise NoInboundUserMessageError(
            f"대표 메시지가 inbound user message가 아니다: id={representative_message_id} sender={sender!r}"
        )

    row = (await session.execute(_ALLOC_SQL, {"user_id": user_id})).first()
    watermark, generation, mode = int(row[0]), int(row[1]), row[2]
    await session.execute(
        _INSERT_TURN_SQL,
        {
            "user_id": user_id,
            "source_watermark": watermark,
            "representative_message_id": representative_message_id,
            "committed_at": committed_at,
        },
    )
    await session.execute(
        _INSERT_TURN_MESSAGE_SQL,
        [{"user_id": user_id, "source_watermark": watermark, "message_id": i} for i in ids],
    )
    return SourceTurn(
        watermark=watermark,
        memory_generation=generation,
        memory_mode=mode,
        message_ids=tuple(ids),
    )


# ─────────────────────────────────────────────────────────────
# legacy 기억 스냅샷 — cutover guard(W10). mode 분기를 **SQL의 WHERE로** 건다.
# ─────────────────────────────────────────────────────────────
# 호출 전에 mode를 읽어 분기하면 그 사이 cutover가 끼어들 수 있다(read-then-write 레이스).
# 같은 문장 안에서 조건을 걸어야 normalized 유저의 스냅샷을 legacy 경로가 덮어쓰지 못한다.
# 마지막 방어선은 트리거(20260804_memory_cutover_guard.sql)이고 이건 두 번째 방어선이다.
_SAVE_LEGACY_SNAPSHOT_SQL = text("""
INSERT INTO chat_contexts (user_id, memory_text, memory_refreshed_at)
VALUES (:user_id, :memory_text, :memory_refreshed_at)
ON CONFLICT (user_id) DO UPDATE
  SET memory_text = EXCLUDED.memory_text,
      memory_refreshed_at = EXCLUDED.memory_refreshed_at,
      updated_at = now()
  WHERE chat_contexts.memory_mode = 'legacy'
RETURNING user_id
""")


async def save_legacy_snapshot(
    session: AsyncSession,
    user_id: uuid.UUID | str,
    *,
    memory_text: str,
    refreshed_at: datetime,
) -> bool:
    """mem0 스냅샷 갱신(legacy 유저만). 저장했으면 True, normalized라 건너뛰었으면 False.

    스냅샷 컬럼만 건드린다(앵커·세대·워터마크 클로버 금지). 커밋하지 않는다 — 호출측 경계에 합류.
    normalized 유저에게 False가 나는 것은 정상 경로다(그 유저의 기억 주입은 관계 프로필이 한다).
    """
    row = (
        await session.execute(
            _SAVE_LEGACY_SNAPSHOT_SQL,
            {"user_id": user_id, "memory_text": memory_text, "memory_refreshed_at": refreshed_at},
        )
    ).first()
    return row is not None


# ─────────────────────────────────────────────────────────────
# 잡 연동 — W7 enqueue API만 쓴다(jobs.py는 고치지 않는다). 전부 호출측 트랜잭션에 합류한다.
# ─────────────────────────────────────────────────────────────
def _source_payload(
    *, memory_generation: int, from_watermark: int, through_watermark: int, message_ids: Sequence[int]
) -> dict:
    return {
        "schema_version": SOURCE_PAYLOAD_SCHEMA_VERSION,
        "memory_generation": memory_generation,
        "source_kind": SOURCE_KIND_CONVERSATION_TURN,
        "source_from_watermark": from_watermark,
        "source_through_watermark": through_watermark,
        "message_ids": list(message_ids),
    }


async def enqueue_extraction(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    memory_generation: int,
    from_watermark: int,
    through_watermark: int,
    message_ids: Sequence[int],
) -> uuid.UUID | None:
    """턴마다 추출 잡을 건다(실행은 워커가 같은 유저 것을 묶어서 한다 — 턴당 LLM 호출 방지).

    dedup key에 generation을 넣어 forget/cutover 이후의 잡이 이전 세대와 합쳐지지 않게 한다.
    """
    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=JOB_MEMORY_EXTRACT,
        user_id=user_id,
        dedup_key=f"{user_id}:{memory_generation}:{from_watermark}:{through_watermark}",
        payload=_source_payload(
            memory_generation=memory_generation,
            from_watermark=from_watermark,
            through_watermark=through_watermark,
            message_ids=message_ids,
        ),
    )


async def enqueue_reconcile(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    memory_generation: int,
    from_watermark: int,
    through_watermark: int,
    message_ids: Sequence[int],
    candidate_payload: dict | None = None,
) -> uuid.UUID | None:
    """extract 성공의 **fenced finalize와 같은 트랜잭션**에서 건다(끊기면 후속이 유실된다).

    `candidate_payload`(= `memory-candidate-v1`)는 extract가 뽑아 **마스킹까지 끝낸** 후보다.
    후보를 담아둘 테이블은 없고 잡 payload가 유일한 durable 채널이라 여기에 싣는다. reconcile은
    이 값을 `memory_candidates.parse_candidates`로 **다시 검증**하므로 스키마는 한 곳에만 있다.
    """
    payload = _source_payload(
        memory_generation=memory_generation,
        from_watermark=from_watermark,
        through_watermark=through_watermark,
        message_ids=message_ids,
    )
    if candidate_payload is not None:
        payload["candidate_payload"] = candidate_payload
    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=JOB_MEMORY_RECONCILE,
        user_id=user_id,
        dedup_key=f"{user_id}:{memory_generation}:{from_watermark}:{through_watermark}",
        payload=payload,
    )


async def enqueue_profile_refresh(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    memory_generation: int,
    input_revision: int,
) -> uuid.UUID | None:
    """관계 프로필(W9) 재생성. dedup key = (generation, revision)이라 같은 입력이면 한 번만 돈다."""
    return await jobs.enqueue(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=JOB_PROFILE_REFRESH,
        user_id=user_id,
        dedup_key=f"{user_id}:{memory_generation}:{input_revision}",
        payload={
            "schema_version": SOURCE_PAYLOAD_SCHEMA_VERSION,
            "memory_generation": memory_generation,
            "relationship_profile_input_revision": input_revision,
        },
    )


# ─────────────────────────────────────────────────────────────
# 쓰기 — 상태 변경 문장은 전부 status='active' 조건을 단다(terminal 불가역).
# ─────────────────────────────────────────────────────────────
_INSERT_FACT_SQL = text("""
INSERT INTO memory_facts
  (user_id, kind, canonical_text, subject, predicate, object_json, event_time,
   status, importance, confidence, content_hash, normalization_version)
VALUES
  (:user_id, :kind, :canonical_text, :subject, :predicate, CAST(:object_json AS jsonb), :event_time,
   'active', :importance, :confidence, :content_hash, :normalization_version)
RETURNING id
""")

_INSERT_EVIDENCE_SQL = text("""
INSERT INTO memory_evidence (fact_id, source_type, source_id, source_excerpt_hash, observed_at)
VALUES (:fact_id, :source_type, :source_id, :source_excerpt_hash, :observed_at)
ON CONFLICT (fact_id, source_type, source_id) DO NOTHING
""")

_REINFORCE_SQL = text("""
UPDATE memory_facts SET confidence=:confidence, updated_at=now()
WHERE id=:fact_id AND user_id=:user_id AND status='active'
RETURNING id
""")

_SUPERSEDE_SQL = text("""
UPDATE memory_facts
SET status='superseded', valid_to=now(), superseded_by=:superseded_by, updated_at=now()
WHERE id=:fact_id AND user_id=:user_id AND status='active'
RETURNING id
""")

_BUMP_REVISION_SQL = text("""
INSERT INTO chat_contexts (user_id, relationship_profile_input_revision)
VALUES (:user_id, 1)
ON CONFLICT (user_id) DO UPDATE
  SET relationship_profile_input_revision =
        chat_contexts.relationship_profile_input_revision + 1
RETURNING relationship_profile_input_revision
""")


async def _insert_fact(
    session: AsyncSession, *, user_id: uuid.UUID | str, decision: Decision, nickname: str | None
) -> uuid.UUID:
    n = decision.normalized
    row = (
        await session.execute(
            _INSERT_FACT_SQL,
            {
                "user_id": user_id,
                "kind": n.kind,
                # 저장 직전 마스킹 강제(자연 멱등 — 후보 파싱에서 이미 통과했어도 무해).
                "canonical_text": memory_norm.mask_for_storage(n.canonical_text, nickname),
                "subject": memory_norm.mask_for_storage(n.subject, nickname),
                "predicate": n.predicate,
                # jsonb 안의 문자열도 저장 직전 마스킹한다. 파싱 단계에서 이미 통과했어도
                # Decision을 파싱 우회로 직접 만들면 실명이 jsonb에 남을 수 있어 방어선을 맞춘다.
                "object_json": (
                    None
                    if n.object_json is None
                    else json.dumps(memory_norm.mask_tree(n.object_json, nickname))
                ),
                "event_time": n.event_time,
                "importance": decision.candidate.importance,
                "confidence": decision.confidence,
                "content_hash": n.content_hash,
                "normalization_version": n.version,
            },
        )
    ).first()
    return row[0]


async def add_evidence(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    fact_id: uuid.UUID,
    message_ids: Sequence[int],
    observed_at: datetime,
    contents: Mapping[int, str] | None = None,
) -> int:
    """근거 insert(dedup). **insert 전에 `messages.user_id == fact.user_id`를 같은 트랜잭션에서 검증**한다.

    FK가 user_id를 안 태우므로 이 검사가 유일한 방어선이다. 하나라도 어긋나면 아무것도 쓰지 않는다.
    """
    if not message_ids:
        return 0
    if contents is None:
        contents = {mid: body for mid, (_, body) in
                    (await _load_messages(session, user_id, message_ids)).items()}
    else:
        missing = [i for i in message_ids if i not in contents]
        if missing:
            raise CrossUserEvidenceError(f"검증된 메시지 본문이 없다: {missing}")
    rows = [
        {
            "fact_id": fact_id,
            "source_type": SOURCE_KIND_CONVERSATION_TURN,
            "source_id": mid,
            # 근거 원문의 지문 — 나중에 메시지가 바뀌었는지(=근거가 흔들렸는지) 대조할 수 있게 한다.
            "source_excerpt_hash": hashlib.sha256(
                (contents[mid] or "").encode("utf-8")
            ).hexdigest(),
            "observed_at": observed_at,
        }
        for mid in dict.fromkeys(message_ids)
    ]
    await session.execute(_INSERT_EVIDENCE_SQL, rows)
    return len(rows)


async def bump_input_revision(session: AsyncSession, user_id: uuid.UUID | str) -> int:
    """`relationship_profile_input_revision` +1. **실제 변경이 있었던 트랜잭션에서 정확히 한 번만** 부른다."""
    row = (await session.execute(_BUMP_REVISION_SQL, {"user_id": user_id})).first()
    return int(row[0])


async def apply_decisions(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    decisions: Sequence[Decision],
    observed_at: datetime,
    nickname: str | None = None,
) -> ApplyResult:
    """판정 결과를 반영한다(커밋하지 않는다 — 잡 finalize와 같은 트랜잭션에 합류).

    IGNORE만 있으면 아무 쓰기도, revision 증가도 없다. terminal fact를 건드리게 되면(동시 forget 등)
    되살리지 않고 `TerminalFactError`로 전체를 되돌린다.
    """
    added = reinforced = superseded = ignored = 0
    evidence_ids = {i for d in decisions for i in d.new_evidence_message_ids}
    # 근거로 쓸 메시지를 **한 번에** 검증·적재한다(모든 evidence insert 이전, 같은 트랜잭션).
    contents = (
        {mid: body for mid, (_, body) in (await _load_messages(session, user_id, sorted(evidence_ids))).items()}
        if evidence_ids
        else {}
    )

    for d in decisions:
        if d.action == ACTION_IGNORE:
            ignored += 1
            continue
        if d.action == ACTION_REINFORCE:
            target = d.target_fact_ids[0]
            row = (
                await session.execute(
                    _REINFORCE_SQL,
                    {"fact_id": target, "user_id": user_id, "confidence": d.confidence},
                )
            ).first()
            if row is None:  # active가 아니다 = 그 사이 forgotten/superseded → 되살리지 않는다
                raise TerminalFactError(f"active가 아닌 fact를 REINFORCE할 수 없다: {target}")
            await add_evidence(
                session,
                user_id=user_id,
                fact_id=target,
                message_ids=d.new_evidence_message_ids,
                observed_at=observed_at,
                contents=contents,
            )
            reinforced += 1
            continue

        if d.action not in (ACTION_ADD, ACTION_KEEP_BOTH, ACTION_SUPERSEDE):
            raise MemoryRepoError(f"알 수 없는 판정: {d.action!r}")

        new_id = await _insert_fact(session, user_id=user_id, decision=d, nickname=nickname)
        await add_evidence(
            session,
            user_id=user_id,
            fact_id=new_id,
            message_ids=d.new_evidence_message_ids,
            observed_at=observed_at,
            contents=contents,
        )
        if d.action == ACTION_SUPERSEDE:
            # 새 행을 **먼저** 만든 뒤 기존 active를 닫는다(superseded_by가 가리킬 대상이 있어야 한다).
            for target in d.target_fact_ids:
                row = (
                    await session.execute(
                        _SUPERSEDE_SQL,
                        {"fact_id": target, "user_id": user_id, "superseded_by": new_id},
                    )
                ).first()
                if row is None:
                    raise TerminalFactError(f"active가 아닌 fact를 SUPERSEDE할 수 없다: {target}")
            superseded += 1
        else:
            added += 1

    changed = bool(added or reinforced or superseded)
    revision = await bump_input_revision(session, user_id) if changed else None
    return ApplyResult(
        changed=changed,
        revision=revision,
        added=added,
        reinforced=reinforced,
        superseded=superseded,
        ignored=ignored,
    )
