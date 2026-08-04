"""잊어줘(forget) — 정규화 기억의 영속 삭제·재유입 차단.

지키는 것(어기면 조용히 깨진다):

1. **순서가 계약이다.** 한 트랜잭션 안에서 `chat_contexts` 행을 `FOR UPDATE`로 잠근 뒤
   closure → generation/revision +1 → marker → fact → insight → profile → enqueue 순으로 처리한다.
   closure를 먼저 영속화해야 retention 후 replay로 같은 구간이 되살아나지 못한다.
3. **terminal 불가역.** 상태 변경 UPDATE는 전부 출발 상태를 WHERE에 못 박고(active/published)
   반대 방향으로 가는 문장이 이 모듈에 **하나도 없다**. marker를 지우는 문장도 없다
   (retention 삭제는 별도 절차이며 marker가 마지막 statement다 — 명세 1338-1341행).
4. **hash 이중 신원 금지.** fact scope marker는 그 행의 `content_hash`/`normalization_version`을
   **그대로 복사**한다. 다시 계산하지 않는다(계산식이 바뀌면 잊은 사실이 되살아난다).
5. **자유문 정규식으로 intent를 보충하지 않는다.** 분류 입력은 control 도구가 만든
   `ControlIntent`뿐이고(W4 계약), 해석할 수 없는 intent는 실행 대상에서 빠진다(fail-closed).

커밋하지 않는다 — 호출측 트랜잭션 경계에 합류한다.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import memory_registry, memory_repo, recall_suppression
from app.services.memory_reconcile import SCOPE_ALL, SCOPE_FACT, SCOPE_PREDICATE

_log = logging.getLogger("moly-backend")

# 결과 코드(계약) — 로그·trace·호출측 응답 분기가 이 값을 본다.
RESULT_APPLIED = "applied"
RESULT_NOTHING_MATCHED = "nothing_matched"   # 대상이 없다(타 유저 id 포함) — 아무것도 쓰지 않았다

CONTROL_INTENT_FORGET = "forget"


class ForgetError(Exception):
    """forget 불변식 위반 — 부분 반영 없이 트랜잭션을 실패시킨다."""


@dataclass(frozen=True, slots=True)
class ForgetRequest:
    """실행 가능한 형태로 해석된 요청 1건. 해석할 수 없는 intent는 여기까지 오지 않는다."""

    scope: str
    fact_ids: tuple[uuid.UUID, ...] = ()
    predicate: str | None = None
    future_learning: str = "allow"

    def __post_init__(self) -> None:
        if self.scope == SCOPE_FACT and not self.fact_ids:
            raise ForgetError("fact scope에는 fact_id가 필요하다")
        if self.scope == SCOPE_PREDICATE and not self.predicate:
            raise ForgetError("predicate scope에는 predicate가 필요하다")
        if self.scope not in (SCOPE_FACT, SCOPE_PREDICATE, SCOPE_ALL):
            raise ForgetError(f"알 수 없는 scope: {self.scope!r}")
        if self.future_learning not in {"allow", "block"}:
            raise ForgetError("future_learning must be allow or block")


@dataclass(frozen=True, slots=True)
class ForgetResult:
    """실행 결과. `status != applied`면 **아무 쓰기도 일어나지 않았다**."""

    status: str
    request: ForgetRequest
    operation_id: uuid.UUID | None = None
    memory_generation: int | None = None
    input_revision: int | None = None
    closure: tuple[int, int] | None = None
    markers: int = 0
    forgotten_facts: tuple[uuid.UUID, ...] = field(default=())
    invalidated_insights: tuple[uuid.UUID, ...] = field(default=())
    invalidated_profiles: tuple[uuid.UUID, ...] = field(default=())
    deleted_checkpoints: int = 0  # 지운 대화 요약 수(파생 기억)

    @property
    def wrote_anything(self) -> bool:
        return self.status == RESULT_APPLIED


# ─────────────────────────────────────────────────────────────
# 분류 — 순수 함수, DB 접근 없음.
# ─────────────────────────────────────────────────────────────
def classify(intent: Any) -> ForgetRequest | None:
    """`ControlIntent` 1건 → 실행 가능한 요청. 해석 못 하면 None(실행하지 않는다).

    - `kind='forget'`만 본다(pin은 이 모듈 소관이 아니다).
    - `target_fact_ids`가 있으면 fact scope.
    - 없고 `value`가 registry의 predicate면 predicate scope.
    - 그 밖(자유문 value 등)은 None이다. **scope='all'은 모델 intent에서 추론하지 않는다** —
      전량 삭제는 명시적 API의 `confirm=true` 경로에서만 허용한다.
    """
    if getattr(intent, "kind", None) != CONTROL_INTENT_FORGET:
        return None
    fact_ids = tuple(getattr(intent, "target_fact_ids", ()) or ())
    if fact_ids:
        return ForgetRequest(
            scope=SCOPE_FACT,
            fact_ids=fact_ids,
            future_learning=getattr(intent, "future_learning", "allow"),
        )
    value = getattr(intent, "value", None)
    if memory_registry.is_predicate(value):
        return ForgetRequest(
            scope=SCOPE_PREDICATE,
            predicate=value,
            future_learning=getattr(intent, "future_learning", "allow"),
        )
    return None


def classify_all(intents: Sequence[Any]) -> list[ForgetRequest]:
    """해석된 요청만 남긴다. 버려진 intent는 계측용으로 로그에 수만 남긴다."""
    out = [r for r in (classify(i) for i in intents) if r is not None]
    dropped = len(intents) - len(out)
    if dropped:
        _log.info("forget intent 해석 불가로 제외 — n=%d", dropped)
    return out


# ─────────────────────────────────────────────────────────────
# SQL — 상태 변경은 전부 출발 상태를 WHERE에 못 박는다(terminal 불가역).
# ─────────────────────────────────────────────────────────────
_LOCK_CONTEXT_SQL = text("""
SELECT memory_generation, memory_source_watermark, relationship_profile_input_revision
FROM chat_contexts WHERE user_id=:user_id FOR UPDATE
""")

# 대상 fact의 marker 재료(hash/version)와 근거 watermark 구간. **status로 거르지 않는다** —
# 이미 superseded된 행도 hash를 marker로 남겨야 같은 내용이 다시 들어오지 못한다.
# watermark를 모르는 근거가 하나라도 있으면 구간을 확정할 수 없다(아래에서 실패시킨다).
_FACT_TARGETS_SQL = text("""
SELECT f.id, f.content_hash, f.normalization_version, f.status,
       COALESCE(array_agg(DISTINCT tm.source_watermark)
                FILTER (WHERE tm.source_watermark IS NOT NULL),'{}') AS watermarks,
       count(e.source_id) AS evidence_n,
       count(tm.source_watermark) AS mapped_n
FROM memory_facts f
LEFT JOIN memory_evidence e ON e.fact_id = f.id
LEFT JOIN memory_source_turn_messages tm
       ON tm.user_id = f.user_id AND tm.message_id = e.source_id
WHERE f.user_id=:user_id AND f.id = ANY(CAST(:ids AS uuid[]))
GROUP BY f.id
""")

_INSERT_CLOSURE_SQL = text("""
INSERT INTO memory_source_closures
  (user_id, source_kind, from_watermark, through_watermark, forget_operation_id)
VALUES (:user_id, :source_kind, :from_watermark, :through_watermark, :operation_id)
ON CONFLICT (user_id, forget_operation_id, source_kind, from_watermark, through_watermark)
DO NOTHING
""")

# generation·revision을 **같은 문장에서** 올려 진행 중인 파생 잡을 모두 stale로 만든다.
_BUMP_SQL = text("""
UPDATE chat_contexts
SET memory_generation = memory_generation + 1,
    relationship_profile_input_revision = relationship_profile_input_revision + 1,
    updated_at = now()
WHERE user_id=:user_id
RETURNING memory_generation, relationship_profile_input_revision
""")

_INSERT_MARKER_SQL = text("""
INSERT INTO memory_forget_markers
  (user_id, scope, fact_id, normalized_hash, normalization_version, predicate,
   memory_generation,cut_watermark,future_learning)
VALUES
  (:user_id, :scope, :fact_id, :normalized_hash, :normalization_version, :predicate,
   :generation,:cut_watermark,:future_learning)
""")

_PREDICATE_WATERMARKS_SQL = text("""
SELECT DISTINCT tm.source_watermark
FROM memory_facts f
JOIN memory_evidence e ON e.user_id=f.user_id AND e.fact_id=f.id
JOIN memory_source_turn_messages tm
  ON tm.user_id=e.user_id AND tm.message_id=e.source_id
WHERE f.user_id=:user_id AND f.predicate=:predicate
ORDER BY tm.source_watermark
""")

# fact id로도, 같은 (version, hash)로도 지운다 — 같은 내용이 다른 행으로 남아 있으면
# marker를 우회해 프로필에 다시 실린다.
_FORGET_FACT_SQL = text("""
UPDATE memory_facts
SET status='forgotten', valid_to=now(), embedding=NULL, updated_at=now()
WHERE user_id=:user_id AND status='active'
  AND (id=:fact_id
       OR (normalization_version=:normalization_version AND content_hash=:normalized_hash))
RETURNING id
""")

_FORGET_PREDICATE_SQL = text("""
UPDATE memory_facts
SET status='forgotten', valid_to=now(), embedding=NULL, updated_at=now()
WHERE user_id=:user_id AND status='active' AND predicate=:predicate
RETURNING id
""")

_FORGET_ALL_FACTS_SQL = text("""
UPDATE memory_facts
SET status='forgotten', valid_to=now(), embedding=NULL, updated_at=now()
WHERE user_id=:user_id AND status='active'
RETURNING id
""")

# 근거가 사라진 통찰은 근거 없는 파생이 된다 → invalidated(terminal).
# 대화 요약(W11)도 파생 기억이다. 요약은 산문이라 **어느 요약에 그 사실이 들었는지 알 수 없으므로**
# 그 유저 것을 전부 지운다 — 남기고 고르는 건 불가능하고, 남기면 잊어달라고 한 내용이 다음 턴
# 프롬프트의 [지난 이야기]로 되살아난다. 요약은 다음 리셋에 다시 만들어지며, 그때는 지금 구간부터
# 시작하므로 잊은 내용이 체인에 복귀하지 않는다.
#
# 원본 messages와 발행된 일기는 지우지 않는다 — 유저가 자기 화면에서 그대로 볼 수 있는 기록이고,
# forget의 대상은 캐피가 "알고 있는 것"(파생 기억)이지 유저의 대화·일기 자체가 아니다.
# 이 경계는 제품 결정 사항(§5 게이트 #5)이며 확정되면 여기 범위도 함께 정해야 한다.
_DELETE_CHECKPOINTS_SQL = text("""
DELETE FROM conversation_checkpoints WHERE user_id = :user_id
""")

_INVALIDATE_INSIGHTS_SQL = text("""
UPDATE memory_insights i
SET status='invalidated', valid_to=now()
WHERE i.user_id=:user_id AND i.status='active'
  AND EXISTS (
    SELECT 1 FROM memory_insight_sources s
    WHERE s.user_id=i.user_id AND s.insight_id=i.id
      AND s.fact_id = ANY(CAST(:fact_ids AS uuid[]))
  )
RETURNING i.id
""")

_INVALIDATE_ALL_INSIGHTS_SQL = text("""
UPDATE memory_insights
SET status='invalidated', valid_to=now()
WHERE user_id=:user_id AND status='active'
RETURNING id
""")

# 잊은 근거를 참조하는 published 프로필은 즉시 무효화한다. 렌더러도 매 턴 근거를 재대조하지만
# (W9) 무효 상태를 DB에 남겨야 다음 refresh가 새 근거로 다시 만든다.
_INVALIDATE_PROFILES_SQL = text("""
UPDATE relationship_profiles p
SET status='invalidated'
WHERE p.user_id=:user_id AND p.status='published'
  AND EXISTS (
    SELECT 1 FROM relationship_profile_sources s
    WHERE s.user_id=p.user_id AND s.relationship_profile_id=p.id
      AND (s.fact_id = ANY(CAST(:fact_ids AS uuid[]))
           OR s.insight_id = ANY(CAST(:insight_ids AS uuid[])))
  )
RETURNING p.id
""")

_INVALIDATE_ALL_PROFILES_SQL = text("""
UPDATE relationship_profiles
SET status='invalidated'
WHERE user_id=:user_id AND status='published'
RETURNING id
""")


# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────
async def apply(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    request: ForgetRequest,
    operation_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> ForgetResult:
    """forget을 원자 적용한다. 커밋하지 않는다 — 호출측 트랜잭션에 합류한다."""
    op_id = operation_id or uuid.uuid4()

    # 0) 유저 행을 잠근다. 이 뒤의 모든 판단은 잠근 값 기준이다.
    row = (await session.execute(_LOCK_CONTEXT_SQL, {"user_id": user_id})).first()
    if row is None:
        return ForgetResult(status=RESULT_NOTHING_MATCHED, request=request)
    source_watermark = int(row[1])

    # 1) 닫을 소스 구간과 marker 재료를 확정한다.
    targets = await _resolve_targets(session, user_id=user_id, request=request)
    if request.scope == SCOPE_FACT and not targets:
        # 타 유저 id이거나 이미 없는 사실 — 아무것도 쓰지 않는다(성공으로 가장하지도 않는다).
        return ForgetResult(status=RESULT_NOTHING_MATCHED, request=request)
    watermarks = await _closure_watermarks(
        session,
        user_id=user_id,
        request=request,
        targets=targets,
        source_watermark=source_watermark,
    )
    closure = (min(watermarks), max(watermarks)) if watermarks else None
    if request.scope == SCOPE_ALL and watermarks:
        ranges = [(1, source_watermark)]
    else:
        ranges = [(watermark, watermark) for watermark in watermarks]
    for from_watermark, through_watermark in ranges:
        await session.execute(
            _INSERT_CLOSURE_SQL,
            {
                "user_id": user_id,
                "source_kind": memory_repo.SOURCE_KIND_CONVERSATION_TURN,
                "from_watermark": from_watermark,
                "through_watermark": through_watermark,
                "operation_id": op_id,
            },
        )

    suppressed = await recall_suppression.apply(
        session,
        user_id=uuid.UUID(str(user_id)),
        operation_id=op_id,
        scope=request.scope,
        cut_watermark=source_watermark,
        future_learning=request.future_learning,
        fact_ids=request.fact_ids,
        predicate=request.predicate,
    )

    # 2) generation·revision +1 — 진행 중인 잡의 결과를 전부 stale로 만든다.
    bumped = (await session.execute(_BUMP_SQL, {"user_id": user_id})).first()
    if bumped is None:
        raise ForgetError(f"기억 generation을 올리지 못했다: user={user_id}")
    generation, revision = int(bumped[0]), int(bumped[1])

    # 3) marker — 영속 deny key. hash/version은 fact 행에서 **복사**한다(재계산 금지).
    markers = await _write_markers(
        session,
        user_id=user_id,
        request=request,
        targets=targets,
        generation=generation,
        cut_watermark=source_watermark,
    )

    # 4~6) fact → insight → profile 순서로 닫는다(파생이 근거보다 먼저 남지 않게).
    forgotten = await _forget_facts(session, user_id=user_id, request=request, targets=targets)
    insights = await _invalidate_insights(
        session, user_id=user_id, request=request, fact_ids=forgotten
    )
    profiles = await _invalidate_profiles(
        session, user_id=user_id, request=request, fact_ids=forgotten, insight_ids=insights
    )

    # 6-b) 대화 요약도 파생 기억이라 함께 지운다(위 _DELETE_CHECKPOINTS_SQL 주석 참조).
    checkpoints = (
        await session.execute(_DELETE_CHECKPOINTS_SQL, {"user_id": user_id})
    ).rowcount or 0

    # 7) 파생 프로필 재생성을 같은 트랜잭션에서 건다. 벡터는 fact UPDATE에서 즉시 NULL 처리됐다.
    await memory_repo.enqueue_profile_refresh(
        session, user_id=user_id, memory_generation=generation, input_revision=revision
    )

    _log.info(
        "forget 적용 — user=%s scope=%s gen=%d rev=%d facts=%d insights=%d profiles=%d ckpt=%d suppressed=%d",
        user_id, request.scope, generation, revision,
        len(forgotten), len(insights), len(profiles), checkpoints, suppressed,
    )
    return ForgetResult(
        status=RESULT_APPLIED,
        request=request,
        operation_id=op_id,
        memory_generation=generation,
        input_revision=revision,
        closure=closure,
        markers=markers,
        forgotten_facts=forgotten,
        invalidated_insights=insights,
        invalidated_profiles=profiles,
        deleted_checkpoints=checkpoints,
    )


@dataclass(frozen=True, slots=True)
class _Target:
    """marker에 복사할 fact 1건 + 그 근거의 watermark 구간."""

    fact_id: uuid.UUID
    content_hash: str
    normalization_version: str
    watermarks: tuple[int, ...]


async def _resolve_targets(
    session: AsyncSession, *, user_id: uuid.UUID | str, request: ForgetRequest
) -> tuple[_Target, ...]:
    """fact scope의 대상 행. 다른 scope는 대상 목록이 필요 없다(구간이 cut watermark다)."""
    if request.scope != SCOPE_FACT:
        return ()
    rows = (
        await session.execute(
            _FACT_TARGETS_SQL,
            {"user_id": user_id, "ids": [str(i) for i in request.fact_ids]},
        )
    ).mappings().all()
    targets: list[_Target] = []
    for r in rows:
        if int(r["evidence_n"]) != int(r["mapped_n"]):
            # 근거가 어느 turn에 속하는지 모르면 닫을 구간을 정할 수 없다 → 부분 삭제 금지.
            raise ForgetError(
                f"근거 watermark를 알 수 없는 fact는 잊을 수 없다: fact_id={r['id']}"
            )
        targets.append(
            _Target(
                fact_id=r["id"],
                content_hash=r["content_hash"],
                normalization_version=r["normalization_version"],
                watermarks=tuple(int(value) for value in (r["watermarks"] or ())),
            )
        )
    return tuple(targets)


async def _closure_watermarks(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    request: ForgetRequest,
    targets: Sequence[_Target],
    source_watermark: int,
) -> tuple[int, ...]:
    """Return exact affected watermarks. Only all-scope intentionally closes 1..cut."""
    if request.scope == SCOPE_FACT:
        return tuple(sorted({value for target in targets for value in target.watermarks}))
    if request.scope == SCOPE_PREDICATE:
        rows = (
            await session.execute(
                _PREDICATE_WATERMARKS_SQL,
                {"user_id": user_id, "predicate": request.predicate},
            )
        ).all()
        return tuple(int(row[0]) for row in rows)
    if source_watermark < 1:
        return ()
    return (1,) if source_watermark == 1 else (1, source_watermark)


async def _write_markers(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    request: ForgetRequest,
    targets: Sequence[_Target],
    generation: int,
    cut_watermark: int,
) -> int:
    """scope별 marker. fact scope는 대상 행마다 hash/version을 **그대로 복사**한다."""
    if request.scope == SCOPE_FACT:
        rows = [
            {
                "user_id": user_id,
                "scope": SCOPE_FACT,
                "fact_id": t.fact_id,
                "normalized_hash": t.content_hash,
                "normalization_version": t.normalization_version,
                "predicate": None,
                "generation": generation,
                "cut_watermark": cut_watermark,
                "future_learning": request.future_learning,
            }
            for t in targets
        ]
    else:
        rows = [
            {
                "user_id": user_id,
                "scope": request.scope,
                "fact_id": None,
                "normalized_hash": None,
                "normalization_version": None,
                "predicate": request.predicate if request.scope == SCOPE_PREDICATE else None,
                "generation": generation,
                "cut_watermark": cut_watermark,
                "future_learning": request.future_learning,
            }
        ]
    if rows:
        await session.execute(_INSERT_MARKER_SQL, rows)
    return len(rows)


async def _forget_facts(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    request: ForgetRequest,
    targets: Sequence[_Target],
) -> tuple[uuid.UUID, ...]:
    """active → forgotten. 이미 terminal인 행은 건드리지 않는다(되살리지도 않는다)."""
    ids: list[uuid.UUID] = []
    if request.scope == SCOPE_FACT:
        for t in targets:
            rows = (
                await session.execute(
                    _FORGET_FACT_SQL,
                    {
                        "user_id": user_id,
                        "fact_id": t.fact_id,
                        "normalization_version": t.normalization_version,
                        "normalized_hash": t.content_hash,
                    },
                )
            ).all()
            ids.extend(r[0] for r in rows)
    elif request.scope == SCOPE_PREDICATE:
        rows = (
            await session.execute(
                _FORGET_PREDICATE_SQL, {"user_id": user_id, "predicate": request.predicate}
            )
        ).all()
        ids.extend(r[0] for r in rows)
    else:
        rows = (await session.execute(_FORGET_ALL_FACTS_SQL, {"user_id": user_id})).all()
        ids.extend(r[0] for r in rows)
    return tuple(dict.fromkeys(ids))


async def _invalidate_insights(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    request: ForgetRequest,
    fact_ids: Sequence[uuid.UUID],
) -> tuple[uuid.UUID, ...]:
    if request.scope == SCOPE_ALL:
        rows = (await session.execute(_INVALIDATE_ALL_INSIGHTS_SQL, {"user_id": user_id})).all()
        return tuple(r[0] for r in rows)
    if not fact_ids:
        return ()
    rows = (
        await session.execute(
            _INVALIDATE_INSIGHTS_SQL,
            {"user_id": user_id, "fact_ids": [str(i) for i in fact_ids]},
        )
    ).all()
    return tuple(r[0] for r in rows)


async def _invalidate_profiles(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    request: ForgetRequest,
    fact_ids: Sequence[uuid.UUID],
    insight_ids: Sequence[uuid.UUID],
) -> tuple[uuid.UUID, ...]:
    if request.scope == SCOPE_ALL:
        rows = (await session.execute(_INVALIDATE_ALL_PROFILES_SQL, {"user_id": user_id})).all()
        return tuple(r[0] for r in rows)
    if not fact_ids and not insight_ids:
        return ()
    rows = (
        await session.execute(
            _INVALIDATE_PROFILES_SQL,
            {
                "user_id": user_id,
                "fact_ids": [str(i) for i in fact_ids],
                "insight_ids": [str(i) for i in insight_ids],
            },
        )
    ).all()
    return tuple(r[0] for r in rows)
