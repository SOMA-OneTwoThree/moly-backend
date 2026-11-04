"""대화 요약 checkpoint(W11)의 **저장소** — SQL과 잡 연동. 순수 로직은 `checkpoint`.

여기서 지키는 것:

1. **커밋하지 않는다.** 전부 호출측 트랜잭션 경계에 합류한다 — producer는 메시지 insert와 같은
   트랜잭션에서 잡을 걸고(§W11-1), handler는 fenced finalize와 같은 트랜잭션에서 checkpoint를 쓴다.
2. **insert는 `ON CONFLICT DO NOTHING`.** UNIQUE(user_id, through_message_id, source_hash)가
   같은 입력의 두 번째 저장을 흡수한다(잡 재실행·중복 소비 멱등).
3. **범위 조회는 그 유저 것만.** 요약 입력에 남의 메시지가 섞이는 경로를 만들지 않는다.
4. **Summary는 Fact가 아니다** — 선행 기억 작업은 실제 normal 원문만 예약한다.
   요약 결과를 기억 추출의 근거로 쓰지 않는다.
"""
from __future__ import annotations

import json
import hashlib
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, replace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.advisory_lock import advisory_xact_lock
from app.models.message import SUMMARY_EXCLUDED_MESSAGE_KINDS
from app.services import checkpoint, jobs, retention_release
from app.services.context_provenance import VerifiedCoverage

_log = logging.getLogger("moly-worker")

# 🔴 **현재 세대의 요약만 돌려준다.** 잊어줘는 요약을 전량 지우지만, 지우는 트랜잭션과 쓰는
# 트랜잭션이 겹치면 한쪽이 다른 쪽의 미커밋 행을 못 본다(READ COMMITTED) — 그 틈으로 들어온
# 요약은 삭제를 피한다. 잠금으로 막으면 챗 Phase 2와 락 순서가 반대라 교착이 나므로,
# **지우는 대신 읽을 때 거른다.** 끼어든 행은 남아 있어도 프롬프트에 실리지 않는다.
#
# `publish_state='published'`도 같은 이유로 여기서 거른다. 이 컬럼은 "아직 쓰면 안 되는 행"을
# 표시하는데, 거르는 조건이 없으면 표시를 붙여 넣은 행이 그대로 프롬프트에 실린다.
# 기존 행은 컬럼 기본값이 `published`라 이 조건에 영향받지 않는다.
_LATEST_SQL = text("""
SELECT c.id, c.through_message_id, c.summary, c.version, c.source_hash
FROM conversation_checkpoints c
JOIN chat_contexts cc
  ON cc.user_id = c.user_id
 AND COALESCE(cc.memory_generation, 0) = c.memory_generation
WHERE c.user_id = :user_id
  AND c.publish_state = 'published'
  AND c.version <> 'conversation-checkpoint-summary-v5'
ORDER BY c.through_message_id DESC, c.created_at DESC, c.id DESC
LIMIT 1
""")

_LATEST_RETENTION_SQL = text(str(_LATEST_SQL).replace(
    "c.source_hash\n", "c.source_hash, c.state_json, c.validation_json\n"
).replace("c.version <> 'conversation-checkpoint-summary-v5'",
          "c.version = 'conversation-checkpoint-summary-v5' AND c.preparation_id IS NULL"))

_PENDING_SQL = text("""
SELECT 1 FROM async_jobs j
  JOIN chat_contexts cc ON cc.user_id = j.user_id
  WHERE j.user_id = :user_id
    AND j.job_type = 'conversation_checkpoint'
    AND j.state IN ('ready', 'running')
    AND (j.payload->>'retention_preparation_id') IS NULL
 AND j.payload->'memory_generation' = to_jsonb(COALESCE(cc.memory_generation, 0))
LIMIT 1
""")


_COUNT_SQL = text("""
SELECT count(*)
FROM conversation_checkpoints c
JOIN chat_contexts cc
  ON cc.user_id = c.user_id
 AND COALESCE(cc.memory_generation, 0) = c.memory_generation
WHERE c.user_id = :user_id
  AND c.publish_state = 'published'
""")

# 요약 잡의 stale 판정 기준. forget이 이 값을 +1 하므로(memory_forget._BUMP_SQL), 잡을 만든
# 시점의 값과 실행 시점의 값이 다르면 그 사이에 "잊어줘"가 있었다는 뜻이다.
_GENERATION_SQL = text(
    "SELECT COALESCE(memory_generation, 0) FROM chat_contexts WHERE user_id = :user_id"
)

# 요약 대상 구간. after_id는 이전 checkpoint의 through(없으면 0) — 열린 하한, 닫힌 상한이다.
_RANGE_SQL = text("""
SELECT id, sender, kind, content, created_at
FROM messages
WHERE user_id = :user_id AND id > :after_id AND id <= :through_id
  AND kind NOT IN ('fortune_context_root','fortune_derived','safety_check','safety')
ORDER BY id
LIMIT :max_rows
""")

_RANGE_ALL_SQL = text(str(_RANGE_SQL).replace(
    "  AND kind NOT IN ('fortune_context_root','fortune_derived','safety_check','safety')\n", ""
))

# 세대 검사를 **저장과 같은 문장 안에** 둔다. 따로 읽고 나서 쓰면 그 사이에 잊어줘가 끼어들어
# 세대 증가·요약 삭제를 마치고, 저장은 검사를 전부 피해 지운 요약을 되살린다(check-then-act).
#
# 한 문장이면 잠금이 필요 없다 — 잠그려 하면 챗 Phase 2와 락 순서가 반대라 교착이 나고,
# 그걸 NOWAIT로 피하면 이번엔 재시도 횟수를 갉아먹어 요약이 영구 유실된다. 검사와 행동을
# 쪼개지 않는 게 근본 해법이다.
_INSERT_SQL = text("""
INSERT INTO conversation_checkpoints
  (user_id, through_message_id, summary, version, source_hash, memory_generation)
SELECT :user_id, :through_message_id, :summary, :version, :source_hash, :expected_generation
WHERE EXISTS (
  SELECT 1 FROM chat_contexts
  WHERE user_id = :user_id AND COALESCE(memory_generation, 0) = :expected_generation
)
ON CONFLICT (user_id, through_message_id, source_hash) DO NOTHING
RETURNING id
""")

_INSERT_RETENTION_SQL = text("""
INSERT INTO conversation_checkpoints
 (user_id,through_message_id,summary,version,source_hash,memory_generation,
  state_json,validation_json,previous_checkpoint_id,coverage_from_message_id,
  coverage_through_message_id,segment_through_message_id,preparation_id,publish_state)
SELECT :user_id,:through_message_id,:summary,:version,:source_hash,:expected_generation,
       CAST(:state AS jsonb),CAST(:validation AS jsonb),CAST(:previous_id AS uuid),
       :coverage_from,:through_message_id,:through_message_id,CAST(:preparation_id AS uuid),:preparation_state
WHERE EXISTS (SELECT 1 FROM chat_contexts cc
              WHERE cc.user_id=:user_id AND cc.memory_generation=:expected_generation)
  AND (SELECT c.id FROM conversation_checkpoints c
       WHERE c.user_id=:user_id AND c.memory_generation=:expected_generation
         AND c.version='conversation-checkpoint-summary-v5' AND c.preparation_id IS NULL AND c.publish_state='published'
       ORDER BY c.through_message_id DESC,c.created_at DESC,c.id DESC LIMIT 1)
      IS NOT DISTINCT FROM CAST(:previous_id AS uuid)
ON CONFLICT (user_id, through_message_id, source_hash) DO NOTHING RETURNING id
""")

_USER_STATE_SQL = text("SELECT nickname, language FROM profiles WHERE id = :user_id")


async def read_memory_generation(session: AsyncSession, user_id: uuid.UUID | str) -> int:
    """그 유저의 현재 기억 세대. 행이 없으면 0(아직 대화 컨텍스트가 없는 유저).

    이 값은 **이른 종료용 힌트**다. 최종 방어는 `insert`가 같은 문장 안에서 하는 세대 검사이며,
    여기서 읽은 값이 낡아도 저장은 안전하다.
    """
    row = (await retention_release.execute(session, _GENERATION_SQL, {"user_id": user_id})).first()
    return int(row[0]) if row is not None else 0



async def load_latest(
    session: AsyncSession, user_id: uuid.UUID | str, *, retention_enabled: bool | None = None,
) -> checkpoint.Checkpoint | None:
    """`through_message_id`가 가장 큰 checkpoint 하나(§W11-5). 없으면 None."""
    enabled = settings.context_retention_v2_enabled if retention_enabled is None else retention_enabled
    if enabled:
        await retention_release.require_binding(session)
    row = (await retention_release.execute(session, _LATEST_RETENTION_SQL if enabled else _LATEST_SQL,
                                 {"user_id": user_id})).first()
    if row is None:
        return None
    value = checkpoint.Checkpoint(
        id=row[0],
        through_message_id=int(row[1]),
        summary=row[2],
        version=row[3],
        source_hash=row[4],
        state_json=row[5] if enabled else None,
        validation_json=row[6] if enabled else None,
    )
    if enabled:
        from app.services.context_retention import RetentionError, validate_progress_sources
        if value.state_json is None or not isinstance(value.validation_json, dict) or value.validation_json.get("processing_complete") is not True:
            raise RetentionError("checkpoint_not_validated")
        await validate_progress_sources(session, user_id=user_id, state=value.state_json)
    return value


async def coverage_for_checkpoint(session, *, user_id, generation: int,
                                  value: checkpoint.Checkpoint | None) -> VerifiedCoverage | None:
    """Grant raw exclusion from the existing validated current-generation reader.

    The caller holds its normal user read lock and obtains ``value`` from
    load_latest, whose SQL joins the current generation. The existing publisher
    proves a complete same-user prefix/parent chain, including policy-excluded
    rows. The assembler then binds only actual fetched SourceRefs to this grant.
    No additional history scan or new persistence/authorization contract.
    """
    from app.services import context_retention
    if value is None or value.version != checkpoint.RETENTION_SUMMARIZER_VERSION:
        return None
    if (not isinstance(value.validation_json, dict)
            or value.validation_json.get("processing_complete") is not True):
        return None
    if not await context_retention.processing_complete(session, user_id=user_id,
            generation=generation, through_id=value.through_message_id):
        return None
    return VerifiedCoverage(uuid.UUID(str(user_id)), generation, uuid.UUID(str(value.id)),
        value.through_message_id, value.source_hash,
        frozenset(value.validation_json.get("policy_excluded_ids", [])))


async def count(session: AsyncSession, user_id: uuid.UUID | str, *, retention_enabled: bool = False) -> int:
    """그 유저의 checkpoint 수 — 몇 번째 checkpoint인지(재검증 주기 판정)에 쓴다."""
    statement = text(str(_COUNT_SQL) + " AND c.version = 'conversation-checkpoint-summary-v5' AND c.preparation_id IS NULL") if retention_enabled else _COUNT_SQL
    row = (await retention_release.execute(session, statement, {"user_id": user_id})).first()
    return int(row[0]) if row is not None else 0


async def load_user_state(
    session: AsyncSession, user_id: uuid.UUID | str
) -> tuple[str | None, str | None] | None:
    """`(nickname, language)`. 행이 없으면 None(탈퇴) — 호출측이 cancelled로 끝낸다."""
    row = (await retention_release.execute(session, _USER_STATE_SQL, {"user_id": user_id})).first()
    return None if row is None else (row[0], row[1])


async def load_range(
    session: AsyncSession,
    user_id: uuid.UUID | str,
    *,
    after_id: int | None,
    through_id: int,
    max_rows: int | None = None,
    include_excluded: bool = False,
) -> list[checkpoint.SourceMessage]:
    """`(after_id, through_id]`의 요약 허용 메시지(placeholder, 실명 렌더 금지).

    `max_rows`는 폭주 방어 상한이다. 상한에 정확히 닿았는지로 "잘렸는지"를 호출측이 판정한다.
    원문 대화 조회와 달리 fortune_*·safety*는 제외한다. 일반 요약과 재점검이 함께 사용한다.
    """
    rows = (
        await retention_release.execute(session,
            _RANGE_ALL_SQL if include_excluded else _RANGE_SQL,
            {
                "user_id": user_id,
                "after_id": after_id or 0,
                "through_id": through_id,
                "max_rows": max_rows or settings.context_hard_msg_cap,
            },
        )
    ).mappings().all()
    return [
        checkpoint.SourceMessage(
            id=int(r["id"]), sender=r["sender"], kind=r["kind"], content=r["content"] or "",
            created_at=r["created_at"],
        )
        for r in rows
        if include_excluded or r["kind"] not in SUMMARY_EXCLUDED_MESSAGE_KINDS
    ]


async def insert(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    through_message_id: int,
    summary: str,
    source_hash: str,
    expected_generation: int,
    version: str = checkpoint.SUMMARIZER_VERSION,
    state_json: dict | None = None,
    validation_json: dict | None = None,
    previous_id: uuid.UUID | None = None,
    coverage_from: int | None = None,
) -> uuid.UUID | None:
    """checkpoint 1행. 이미 같은 `(user, through, source_hash)`가 있으면 None(멱등).

    `expected_generation`이 지금 값과 다르면 **아무것도 쓰지 않고** None을 돌려준다 —
    그 사이에 잊어줘가 있었다는 뜻이고, 그 대화를 다시 요약해 넣으면 안 된다.
    """
    if version == checkpoint.RETENTION_SUMMARIZER_VERSION:
        await retention_release.require_binding(session, lock=True)
    row = (
        await retention_release.execute(session,
            _INSERT_RETENTION_SQL if version == checkpoint.RETENTION_SUMMARIZER_VERSION else _INSERT_SQL,
            {
                "user_id": user_id,
                "through_message_id": through_message_id,
                "summary": summary,
                "version": version,
                "source_hash": source_hash,
                "expected_generation": expected_generation,
                "state": json.dumps(state_json, ensure_ascii=False),
                "validation": json.dumps(validation_json, ensure_ascii=False),
                "previous_id": str(previous_id) if previous_id else None,
                "coverage_from": coverage_from,
                "preparation_id": retention_release.preparation_id(),
                "preparation_state": "ready" if retention_release.binding() is not None and retention_release.binding().building else "published",
            },
        )
    ).first()
    return row[0] if row is not None else None


async def enqueue_checkpoint(
    session: AsyncSession, *, user_id: uuid.UUID | str, plan: checkpoint.CheckpointPlan,
    delay_s: float = 0,
) -> uuid.UUID | None:
    """요약 잡 등록(커밋하지 않는다 — 메시지 insert와 **같은 트랜잭션**에서 불러야 한다).

    dedup key가 `(through, source_hash, summarizer)`를 담아 같은 입력은 한 번만 돈다(§W11-3).
    """
    if plan.version == checkpoint.RETENTION_SUMMARIZER_VERSION:
        from app.services import context_retention
        epoch = plan.privacy_epoch
        if epoch is None:
            epoch = await context_retention.capture_privacy_epoch(session, user_id=user_id)
        if epoch is None or not await context_retention.privacy_epoch_matches(
                session, user_id=user_id, privacy_epoch=epoch):
            return None
        plan = replace(plan, privacy_epoch=epoch)
    payload = plan.payload()
    if plan.version == checkpoint.RETENTION_SUMMARIZER_VERSION:
        # Only a newly created regular root adopts packing. Shared source-plan
        # comparisons and existing partial/completed jobs retain their format.
        payload["packing_version"] = checkpoint.PACKING_VERSION
    made = await jobs.enqueue(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT,
        user_id=user_id,
        dedup_key=plan.dedup_key(user_id),
        payload=payload,
        **({"available_at": datetime.now(timezone.utc)+timedelta(seconds=delay_s)} if delay_s else {}),
    )
    if made is not None and plan.version == checkpoint.RETENTION_SUMMARIZER_VERSION:
        await reserve_checkpoint_dependencies(session, user_id=user_id, plan=plan)
    return made


async def reserve_checkpoint_dependencies(session, *, user_id, plan: checkpoint.CheckpointPlan) -> None:
    """Queue only this fixed snapshot's dependencies, in the producer's commit.

    No model/provider call. The caller holds the user lock; the exact ingest
    wake only tries SKIP LOCKED job rows, so it never waits job→user or user→job.
    """
    from app.services import context_retention
    if not await context_retention.enabled_now(session, lock=True):
        return
    if not await context_retention.adopted_user(session, user_id=user_id, lock=True):
        return
    if not await context_retention.privacy_epoch_matches(session, user_id=user_id, privacy_epoch=plan.privacy_epoch):
        return
    await context_retention.enqueue_contract_processing(session, user_id=user_id,
        generation=plan.memory_generation, through_id=plan.through_message_id, privacy_epoch=plan.privacy_epoch)
    await wake_checkpoint_ingest(session, user_id=user_id, generation=plan.memory_generation,
                                 through_id=plan.through_message_id)


async def maybe_enqueue(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    messages: Sequence[checkpoint.SourceMessage],
    keep_from_message_id: int | None = None,
    reset_triggered: bool = False,
    temporal_enabled: bool = False,
    retention_enabled: bool | None = None,
    pressure_through_message_id: int | None = None,
) -> uuid.UUID | None:
    """세그먼트가 트리거에 닿았으면 요약 잡을 건다. 아니면(또는 킬스위치 off면) None.

    `messages`는 **현재 세그먼트 전체**(앵커 이후 + 이번 턴에 insert한 메시지)다. head만 넘기면
    트리거 판정이 tail을 못 봐서 영영 닿지 않는다.

    `keep_from_message_id`는 보존 tail의 첫 메시지 = **새 앵커**다(챗 배선은 항상 넘긴다).
    `checkpoint.plan` docstring 참조 — 요약 경계와 프롬프트에 남는 구간이 이 값으로 맞물린다.

    `reset_triggered=True`면 호출측이 필터 전 세그먼트로 이미 리셋을 확정했다는 뜻이다. 이때는
    필터 후 메시지가 자체 트리거에 못 미쳐도 같은 `keep_from_message_id` 앞의 정상 메시지를
    checkpoint로 보존한다.
    """
    enabled = settings.context_retention_v2_enabled if retention_enabled is None else retention_enabled
    if not settings.context_checkpoint_enabled and not enabled:
        return None
    if enabled:
        return await _maybe_enqueue_retention(session, user_id=user_id, messages=messages,
            keep_from_message_id=keep_from_message_id, reset_triggered=reset_triggered,
            pressure_through_message_id=pressure_through_message_id)
    if not messages or (not reset_triggered and not checkpoint.should_checkpoint(
        messages, retention_enabled=enabled,
    )):
        return None
    # Only reset/pressure turns reach here. Existing async_jobs_user_idx bounds the
    # lookup; terminal or old-generation work must not prevent a new attempt.
    if (await retention_release.execute(session, _PENDING_SQL, {"user_id": user_id})).first() is not None:
        return None
    previous = await load_latest(session, user_id, retention_enabled=True) if enabled else await load_latest(session, user_id)
    # 잡을 만드는 시점의 세대를 payload에 박는다. forget이 이 값을 +1 하므로, forget 이후에
    # 뒤늦게 실행된 잡은 핸들러에서 stale로 끊긴다 — 안 그러면 지운 대화를 다시 요약해 넣는다.
    generation = await read_memory_generation(session, user_id)
    plan = checkpoint.plan(
        messages, preparation_id=retention_release.preparation_id(),
        previous=previous,
        keep_from_message_id=keep_from_message_id,
        memory_generation=generation,
        reset_triggered=reset_triggered,
        version=(checkpoint.RETENTION_SUMMARIZER_VERSION if enabled else
                 checkpoint.SUMMARIZER_VERSION if temporal_enabled
                 else checkpoint.LEGACY_SUMMARIZER_VERSION),
    )
    if plan is None:
        return None
    return await enqueue_checkpoint(session, user_id=user_id, plan=plan)


_RETENTION_PENDING = text("""
SELECT j.id,j.state,j.attempt,j.created_at,j.payload,cc.retention_pressure_through_message_id
FROM async_jobs j JOIN chat_contexts cc ON cc.user_id=j.user_id
WHERE j.user_id=:user_id AND j.job_type='conversation_checkpoint'
  AND j.state IN ('ready','running')
  AND (j.payload->>'retention_preparation_id') IS NULL
 AND j.payload->'memory_generation'=to_jsonb(cc.memory_generation)
ORDER BY j.created_at,j.id LIMIT 1
""")
_MERGE_RETENTION_READY = text("""
UPDATE async_jobs SET payload=CAST(:payload AS jsonb),payload_hash=:payload_hash,
 dedup_key=:dedup_key,available_at=LEAST(created_at+interval '15 seconds',now()+interval '5 seconds')
WHERE id=:id AND state='ready' AND attempt=0
  AND created_at>now()-interval '15 seconds'
RETURNING id
""")
_RETENTION_COOLDOWN = text("""
SELECT EXISTS(SELECT 1 FROM conversation_checkpoints c JOIN chat_contexts cc ON cc.user_id=c.user_id
 WHERE c.user_id=:user_id AND c.memory_generation=cc.memory_generation
   AND c.version='conversation-checkpoint-summary-v5' AND c.preparation_id IS NULL AND c.publish_state='published'
   AND c.created_at>now()-interval '60 seconds')
""")
_SAVE_RETENTION_TARGET = text("""
UPDATE chat_contexts SET pending_anchor_message_id=GREATEST(COALESCE(pending_anchor_message_id,0),:keep_from)
WHERE user_id=:user_id
""")


async def _maybe_enqueue_retention(session: AsyncSession, *, user_id,
    messages: Sequence[checkpoint.SourceMessage], keep_from_message_id, reset_triggered: bool,
    pressure_through_message_id: int | None = None,
) -> uuid.UUID | None:
    if not messages:
        return None
    pressure = pressure_through_message_id is not None
    if not pressure and not reset_triggered and not checkpoint.should_checkpoint(messages, retention_enabled=True):
        return None
    # 모델 호출 없는 예약 트랜잭션에서만 사용자 처리 권한을 잡는다.
    await advisory_xact_lock(session, user_id)
    previous = await load_latest(session, user_id, retention_enabled=True)
    generation = await read_memory_generation(session, user_id)
    plan = checkpoint.plan(messages, previous=previous, preparation_id=retention_release.preparation_id(), keep_from_message_id=keep_from_message_id,
        memory_generation=generation, reset_triggered=reset_triggered,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION,
        through_message_id=pressure_through_message_id,
        reason="input_pressure" if pressure else None)
    if plan is None:
        return None
    if (keep_from_message_id is not None and not pressure
            and not (retention_release.binding() is not None and retention_release.binding().building)):
        await retention_release.execute(session, _SAVE_RETENTION_TARGET, {"user_id": user_id, "keep_from": keep_from_message_id})
    pending = (await retention_release.execute(session, _RETENTION_PENDING, {"user_id": user_id})).first()
    if pending is not None:
        payload = pending[4] if isinstance(pending[4], dict) else json.loads(pending[4])
        if (not pressure and (len(pending)<6 or pending[5] is None)
                and payload.get("reason") != "input_pressure"
                and payload.get("mode") is None and "progress_state" not in payload
                and not any(key in payload for key in (
                    "packing_chunk_cap", "model_attempts_remaining", "progress_page_proof"))
                and payload.get("packing_version") in (None, checkpoint.PACKING_VERSION)
                and pending[1] == "ready" and int(pending[2]) == 0
                and plan.through_message_id > int(payload.get("through_message_id", 0))):
            # An existing fixed snapshot may not be rebound to a newer epoch.
            from app.services import context_retention
            epoch = payload.get("privacy_epoch")
            if not await context_retention.privacy_epoch_matches(session, user_id=user_id, privacy_epoch=epoch):
                return None
            plan = replace(plan, privacy_epoch=epoch)
            merged_payload = plan.payload()
            for key in ("retention_preparation_id", "retention_preparation_building"):
                if key in payload:
                    merged_payload[key] = payload[key]
            if "packing_version" in payload:
                merged_payload["packing_version"] = payload["packing_version"]
            wire = json.dumps(merged_payload, sort_keys=True, separators=(",", ":"))
            merged = (await retention_release.execute(session, _MERGE_RETENTION_READY, {
                "id": pending[0], "payload": wire,
                "payload_hash": hashlib.sha256(wire.encode()).hexdigest(),
                "dedup_key": retention_release.dedup_key(plan.dedup_key(user_id)),
            })).first()
            if merged is not None:
                await reserve_checkpoint_dependencies(session, user_id=user_id, plan=plan)
                return merged[0]
            return None
        return None
    if not pressure and await retention_release.scalar(session, _RETENTION_COOLDOWN, {"user_id": user_id}):
        return None
    return await enqueue_checkpoint(session, user_id=user_id, plan=plan, delay_s=0 if pressure else 5)


@dataclass(frozen=True, slots=True)
class PressureReservation:
    status: str
    through_message_id: int | None = None
    job_id: uuid.UUID | None = None


_WAKE_CHECKPOINT_INGEST = text("""
WITH candidate AS (
  SELECT j.id
  FROM async_jobs j
  JOIN chat_contexts cc ON cc.user_id=j.user_id
  JOIN memory_pipeline_states p ON p.user_id=j.user_id
  LEFT JOIN privacy_subject_barriers b ON b.user_id=j.user_id
  WHERE j.user_id=:user_id AND cc.memory_generation=:generation
    AND p.mode='v2' AND p.bootstrap_status='ready'
    AND p.ingest_through_turn_seq=:cursor AND p.repair_generation=:repair_generation
    AND p.privacy_epoch=:privacy_epoch
    AND j.job_type='mem0_ingest' AND j.queue='memory'
    AND j.dedup_key=:dedup_key
    AND j.payload->'privacy_epoch'=to_jsonb(p.privacy_epoch)
    AND j.state='ready' AND j.attempt=0 AND j.attempt<j.max_attempts
    AND j.lease_owner IS NULL AND j.lease_token IS NULL AND j.lease_until IS NULL
    AND j.payload_redacted_at IS NULL
    AND (j.payload_expires_at IS NULL OR j.payload_expires_at>now())
    AND (j.expires_at IS NULL OR j.expires_at>now())
    AND j.available_at>now()
    AND ((b.user_id IS NULL AND :allow_missing_barrier)
      OR (b.state='active' AND b.epoch=p.privacy_epoch))
    AND EXISTS (
      SELECT 1 FROM messages m
      WHERE m.user_id=j.user_id AND m.kind='normal' AND m.id<=:through_id
        AND m.turn_seq>p.ingest_through_turn_seq
        AND m.turn_seq<=p.source_through_turn_seq
        AND j.payload->'turn_seq'=to_jsonb(m.turn_seq)
    )
  ORDER BY j.created_at,j.id
  FOR UPDATE OF j SKIP LOCKED LIMIT 1
)
UPDATE async_jobs j SET available_at=LEAST(j.available_at,now())
FROM candidate c WHERE j.id=c.id
RETURNING j.id
""")


async def wake_checkpoint_ingest(session: AsyncSession, *, user_id, generation: int,
                                 through_id: int) -> bool:
    """Skip the ordinary idle delay only for an unattempted exact dependency.

    Caller owns a short transaction, optionally an already-held user lock.
    The candidate job is SKIP LOCKED: never wait while holding a user/finalize
    lock. This changes only available_at, never attempts, payload or job state.
    A retry's backoff, a running claim, and exhausted work are not accelerated.
    """
    if retention_release.binding() is not None and retention_release.binding().building:
        return False
    from app.services import context_retention, memory_pipeline
    if type(generation) is not int or generation < 0 or type(through_id) is not int or through_id <= 0:
        raise ValueError("checkpoint_ingest_boundary")
    if not await context_retention.enabled_now(session, lock=True):
        return False
    if not await context_retention.adopted_user(session, user_id=user_id, lock=True):
        return False
    state = await memory_pipeline.load(session, user_id)
    if (state.mode != "v2" or state.bootstrap_status != "ready"
            or state.ingest_through_turn_seq >= state.source_through_turn_seq):
        return False
    params = {
        "user_id": user_id, "generation": generation, "through_id": through_id,
        "cursor": state.ingest_through_turn_seq, "repair_generation": state.repair_generation,
        "privacy_epoch": state.privacy_epoch,
        # Match privacy.authorize_job: missing rows are compatible during
        # backfill, but never authorized after enforced mode is selected.
        "allow_missing_barrier": settings.privacy_barrier_mode != "enforced",
        "dedup_key": memory_pipeline.ingest_dedup_key(
            user_id, state.ingest_through_turn_seq, generation=state.repair_generation,
        ),
    }
    return (await retention_release.execute(session, _WAKE_CHECKPOINT_INGEST, params)).first() is not None


_PRESSURE_BOUNDARY = text("""
SELECT m.id FROM messages m JOIN chat_contexts cc ON cc.user_id=m.user_id
WHERE m.user_id=:user_id AND cc.memory_generation=:generation
  AND m.sender='moly' AND m.turn_position=2 AND m.turn_seq IS NOT NULL
  AND ((CAST(:through_id AS bigint) IS NULL AND m.turn_seq<=cc.last_committed_turn_seq)
       OR (m.id=CAST(:through_id AS bigint) AND m.turn_seq<=cc.last_committed_turn_seq+1))
  AND EXISTS (SELECT 1 FROM messages u WHERE u.user_id=m.user_id AND u.turn_seq=m.turn_seq
              AND u.sender='user' AND u.turn_position=1 AND u.id<m.id)
ORDER BY m.id DESC LIMIT 1
""")
_LOCK_PRESSURE_PIPELINE = text("""
SELECT p.user_id FROM memory_pipeline_states p
JOIN chat_contexts cc ON cc.user_id=p.user_id
WHERE p.user_id=:user_id AND cc.memory_generation=:generation
  AND p.mode='v2' AND p.bootstrap_status='ready'
  AND p.source_through_turn_seq>p.ingest_through_turn_seq
FOR UPDATE OF p SKIP LOCKED
""")
_MISSING_PRESSURE_INGEST = text("""
SELECT EXISTS (
  SELECT 1 FROM messages m
  WHERE m.user_id=:user_id AND m.kind='normal' AND m.turn_seq=:next_turn
    AND m.sender='moly' AND m.turn_position=2 AND m.id<=:through_id
) AND NOT EXISTS (
  SELECT 1 FROM async_jobs j WHERE j.user_id=:user_id AND j.job_type='mem0_ingest'
    AND (j.state IN ('ready','running') OR j.dedup_key=:dedup_key
         OR left(j.dedup_key,length(:dedup_key)+1)=:dedup_key||':'
         OR left(j.dedup_key,length(:resume_prefix))=:resume_prefix)
)
""")


async def ensure_pressure_ingest(session: AsyncSession, *, user_id, generation: int,
                                 through_id: int) -> bool:
    """Fill a missing chain link, never replay an existing ingest family.

    The caller holds the short pressure/user transaction. A busy pipeline is
    skipped rather than waiting behind a worker that owns a job row. Existing
    ready/running work and terminal keys retain their original failure budget.
    """
    if retention_release.binding() is not None and retention_release.binding().building:
        return False
    from app.services import context_retention, memory_pipeline
    if not await context_retention.enabled_now(session, lock=True):
        return False
    if not await context_retention.adopted_user(session, user_id=user_id, lock=True):
        return False
    locked = (await retention_release.execute(session, _LOCK_PRESSURE_PIPELINE, {
        "user_id": user_id, "generation": generation,
    })).first()
    if locked is None:
        return False
    state = await memory_pipeline.load(session, user_id)
    if not await context_retention.privacy_epoch_matches(session, user_id=user_id,
            privacy_epoch=state.privacy_epoch, lock=True):
        return False
    nxt = await memory_pipeline.next_ingest_turn(session, user_id,
                                                cursor=state.ingest_through_turn_seq)
    if nxt is None:
        return False
    key = memory_pipeline.ingest_dedup_key(user_id, state.ingest_through_turn_seq,
                                          generation=state.repair_generation)
    if not await retention_release.scalar(session, _MISSING_PRESSURE_INGEST, {
        "user_id": user_id, "next_turn": nxt, "through_id": through_id, "dedup_key": key,
        "resume_prefix": f"resume:{user_id}:c{state.ingest_through_turn_seq}:g{state.repair_generation}:",
    }):
        return False
    return await memory_pipeline.enqueue_ingest(session, user_id, turn_seq=nxt,
        cursor=state.ingest_through_turn_seq, privacy_epoch=state.privacy_epoch,
        generation=state.repair_generation, delay_s=0) is not None


_SET_PRESSURE = text("""
UPDATE chat_contexts cc
SET retention_pressure_through_message_id=GREATEST(
    COALESCE(cc.retention_pressure_through_message_id,0),:through_id)
WHERE cc.user_id=:user_id AND cc.memory_generation=:generation
  AND NOT EXISTS (SELECT 1 FROM privacy_subject_barriers b
                  WHERE b.user_id=cc.user_id AND b.state<>'active')
RETURNING retention_pressure_through_message_id
""")
_CLEAR_PRESSURE = text("""
WITH completed AS (
SELECT cc.user_id FROM chat_contexts cc
WHERE cc.user_id=:user_id AND cc.memory_generation=:generation
  AND cc.retention_pressure_through_message_id<=:through_id
  AND EXISTS (SELECT 1 FROM conversation_checkpoints c WHERE c.user_id=cc.user_id
    AND c.memory_generation=cc.memory_generation AND c.publish_state='published'
    AND c.version='conversation-checkpoint-summary-v5' AND c.preparation_id IS NULL
    AND c.through_message_id>=cc.retention_pressure_through_message_id
    AND c.validation_json->'processing_complete'='true'::jsonb)
FOR UPDATE OF cc SKIP LOCKED
)
UPDATE chat_contexts cc SET retention_pressure_through_message_id=NULL
FROM completed WHERE cc.user_id=completed.user_id
RETURNING cc.user_id
""")


async def clear_completed_pressure(session: AsyncSession, *, user_id, generation: int,
                                   through_id: int) -> bool:
    """검증된 게시만 해제한다. 바쁜 사용자 행은 기다리지 않고 sweep에 맡긴다.

    worker finalize는 이미 job 행을 잡고 있다. cc 잠금을 기다리면 채팅의 cc→job
    순서와 교착할 수 있으므로 잠기지 않은 행만 해제한다.
    """
    return (await retention_release.execute(session, _CLEAR_PRESSURE, {
        "user_id": user_id, "generation": generation, "through_id": through_id,
    })).first() is not None


async def load_pressure_sources(session: AsyncSession, user_id, *, after_id: int,
                                through_id: int) -> list[checkpoint.SourceMessage]:
    """고정 목표의 연속 앞부분을 읽는다. 마지막 완결 답변까지만 한 작업에 포함한다."""
    sources = []
    cursor = after_id
    for _ in range(8):
        page = await load_range(session, user_id, after_id=cursor, through_id=through_id,
                                max_rows=120, include_excluded=True)
        if not page:
            break
        if any(m.id<=cursor or m.id>through_id for m in page) or any(
                a.id>=b.id for a,b in zip(page,page[1:])):
            raise checkpoint.CheckpointError("pressure_source_order")
        sources.extend(page)
        cursor = page[-1].id
        if cursor>=through_id or len(page)<120 or sum(len(m.content.encode())+24 for m in sources)>=12_288:
            break
    completed = [index for index, source in enumerate(sources) if source.sender == "moly"]
    return sources[:completed[-1]+1] if completed else []


async def request_pressure(session: AsyncSession, *, user_id, generation: int,
                           through_id: int | None = None) -> PressureReservation:
    """커밋하지 않는다. 호출측이 lease/privacy를 재검증하고 사용자 잠금을 먼저 잡는다.

    명시 경계는 같은 커밋에 저장할 양쪽 메시지 flush 이후에만 전달한다.
    일반 요청 실패에서는 경계를 생략하여 이전 커밋 완료 대화만 처리한다.
    """
    from app.services import context_retention
    if type(generation) is not int or generation<0 or (
            through_id is not None and (type(through_id) is not int or through_id<=0)):
        raise ValueError("pressure_boundary")
    if not await context_retention.enabled_now(session, lock=True):
        return PressureReservation("disabled")
    if not await context_retention.adopted_user(session, user_id=user_id, lock=True):
        return PressureReservation("not_adopted")
    boundary = await retention_release.scalar(session, _PRESSURE_BOUNDARY, {
        "user_id": user_id, "generation": generation, "through_id": through_id,
    })
    if boundary is None:
        return PressureReservation("no_source")
    previous = await load_latest(session, user_id, retention_enabled=True)
    if previous is not None and previous.through_message_id>=int(boundary):
        if await context_retention.processing_complete(session, user_id=user_id,
                generation=generation, through_id=previous.through_message_id):
            await clear_completed_pressure(session, user_id=user_id, generation=generation,
                                           through_id=previous.through_message_id)
        return PressureReservation("covered", int(boundary))
    target = await retention_release.scalar(session, _SET_PRESSURE, {
        "user_id": user_id, "generation": generation, "through_id": int(boundary),
    })
    if target is None:
        return PressureReservation("stale_generation")
    # A pending fixed snapshot must not hide the newer durable goal's ingest.
    # Fill only a genuinely absent chain link; all existing failure budgets stay.
    await ensure_pressure_ingest(session, user_id=user_id, generation=generation,
                                 through_id=int(target))
    await wake_checkpoint_ingest(session, user_id=user_id, generation=generation,
                                through_id=int(target))
    # 실행 중 원본 payload와 준비 중인 normal 계획도 변경하지 않는다.
    if (await retention_release.execute(session, _RETENTION_PENDING, {"user_id": user_id})).first() is not None:
        return PressureReservation("pending", int(target))
    sources = await load_pressure_sources(session, user_id,
        after_id=previous.through_message_id if previous else 0, through_id=int(target))
    if not sources:
        return PressureReservation("pending", int(target))
    made = await maybe_enqueue(session, user_id=user_id, messages=sources,
        retention_enabled=True, pressure_through_message_id=sources[-1].id)
    return PressureReservation("queued" if made else "pending", int(target), made)


_TRY_PRESSURE_USER = text("SELECT pg_try_advisory_xact_lock(hashtextextended(CAST(:user_id AS text),0))")
_REMAINING_PRESSURE_GOAL = text("""
SELECT cc.retention_pressure_through_message_id FROM chat_contexts cc
WHERE cc.user_id=:user_id AND cc.memory_generation=:generation
  AND cc.retention_pressure_through_message_id>:published_through
  AND EXISTS(SELECT 1 FROM conversation_checkpoints c WHERE c.user_id=cc.user_id
    AND c.memory_generation=cc.memory_generation AND c.version='conversation-checkpoint-summary-v5' AND c.preparation_id IS NULL
    AND c.publish_state='published' AND c.through_message_id>=:published_through
    AND c.validation_json->'processing_complete'='true'::jsonb)
FOR UPDATE OF cc SKIP LOCKED
""")
_FOLLOWUP_TIMEOUTS = text("SELECT current_setting('lock_timeout'),current_setting('statement_timeout')")
_FOLLOWUP_SET_TIMEOUTS = text("SELECT set_config('lock_timeout',:lock_timeout,true),set_config('statement_timeout',:statement_timeout,true)")


async def reserve_remaining_pressure(session: AsyncSession, *, user_id, generation: int,
                                     published_through_id: int, privacy_epoch: int) -> bool:
    """One optional follow-up after a real publication; never extend the saved goal.

    Finalize already owns a job row. Both user and cc acquisition are nonblocking.
    A savepoint and short SQL timeouts keep optional reservation errors separate
    from the validated publication. The durable goal/sweep remain the fallback.
    """
    from app.services import context_retention
    made = False
    try:
        async with session.begin_nested():
            timeouts = (await retention_release.execute(session, _FOLLOWUP_TIMEOUTS)).first()
            await retention_release.execute(session, _FOLLOWUP_SET_TIMEOUTS,
                {"lock_timeout": "100ms", "statement_timeout": "750ms"})
            if await retention_release.scalar(session, _TRY_PRESSURE_USER, {"user_id": str(user_id)}):
                goal = (await retention_release.execute(session, _REMAINING_PRESSURE_GOAL, {
                    "user_id": user_id, "generation": generation,
                    "published_through": published_through_id,
                })).first()
                if (goal is not None and await context_retention.enabled_now(session, lock=True)
                        and await context_retention.adopted_user(session, user_id=user_id, lock=True)
                        and await context_retention.privacy_epoch_matches(session, user_id=user_id,
                            privacy_epoch=privacy_epoch, lock=True)):
                    reserved = await request_pressure(session, user_id=user_id, generation=generation,
                                                      through_id=int(goal[0]))
                    made = reserved.job_id is not None
            await retention_release.execute(session, _FOLLOWUP_SET_TIMEOUTS,
                {"lock_timeout": timeouts[0], "statement_timeout": timeouts[1]})
        return made
    except Exception as exc:
        _log.warning("checkpoint_pressure_followup_deferred error=%s", type(exc).__name__)
        return False
