"""멈춘 기억 파이프라인을 다시 흐르게 한다.

## 왜 필요한가

ingest는 **체인**이다. 성공한 잡이 다음 잡을 만들고, 챗은 `ingest >= source`일 때만 새 잡을
건다(`chat.py`의 live ingest 조건). 그래서 잡이 하나 `dead`가 되면:

  ingest 커서가 source보다 뒤에 멈춘다 → 챗이 새 잡을 안 건다 → **그 사용자의 기억은
  운영자가 손으로 개입할 때까지 영원히 멈춘다.**

그리고 dead까지 가는 길이 짧다. memory 큐는 재시도 3회에 backoff 2·4초라 **첫 실패로부터
약 6초**면 끝난다(실측). provider 장애는 보통 그보다 길다. 즉 짧은 장애 한 번이 사용자별
기억을 영구 정지시킬 수 있고, 증상은 에러가 아니라 **침묵**이다.

과거 대화 백필처럼 잡을 한 번에 수만 건 흘릴 때는 이 위험이 특히 크다.

## 무엇을 하는가

죽은 ingest 잡을 **replay**한다. 그냥 다시 enqueue하면 안 된다 — `ingest_dedup_key`는
(user, turn)만으로 정해지고 `enqueue`의 ON CONFLICT는 잡 상태와 무관하게 영구적이라,
한 번 죽은 turn은 어떤 코드로도 다시 걸 수 없다(실측: 재시도 enqueue가 None을 반환한다).
`replay_dead`는 원본을 그대로 두고 `replay_of`로 연결된 새 잡을 만들어 이 벽을 넘는다.

이미 replay된 잡은 다시 만들지 않으므로 **여러 번 돌아도 안전**하다.
지우거나 되돌리지 않는다 — 멈춘 곳에서 다시 출발시킬 뿐이다.
"""
from __future__ import annotations

import logging
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import (
    checkpoint, checkpoint_repo, context_retention,
    diary_recall_repo, diary_shared_embeddings, jobs, memory_pipeline, retention_release,
)
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobResult

_log = logging.getLogger("moly-worker")

JOB_MEMORY_SWEEP = memory_pipeline.JOB_MEMORY_SWEEP

# replay operation_id를 원본 job_id에서 결정적으로 만든다 — 같은 잡을 두 번 훑어도 한 번만 산다.
_REPLAY_NAMESPACE = uuid.UUID("6f9b1d2e-0000-4000-8000-000000000001")

# 한 번에 되살릴 사용자 수. 크게 잡으면 한 틱이 길어지고, 작으면 회복이 느리다.
SWEEP_LIMIT = 50

# 다시 걸기까지 기다리는 시간(분). 채팅이 건 잡은 3분 뒤에 도므로 그보다 넉넉히 둔다.
STALL_GRACE_MIN = 30

# 기존 분당 sweep가 추가 발화 없이 후속 요약을 예약한다. 페이지 상한은 조회 안전 제한이며,
# 그 뒤의 원문을 처리 완료로 표시하지 않는다. 다음 sweep가 같은 연속 경계부터 이어 읽는다.
RETENTION_SOURCE_PAGE_SIZE = 120
RETENTION_SOURCE_MAX_PAGES = 8
RETENTION_SOURCE_BYTE_BUDGET = 12_288

_RETENTION_RESUME_USERS = text("""
SELECT cc.user_id, cc.memory_generation
FROM chat_contexts cc
JOIN profiles p ON p.id=cc.user_id
LEFT JOIN LATERAL (
  SELECT c.through_message_id,c.created_at
  FROM conversation_checkpoints c
  WHERE c.user_id=cc.user_id AND c.memory_generation=cc.memory_generation
    AND c.version='conversation-checkpoint-summary-v5' AND c.publish_state='published'
  ORDER BY c.through_message_id DESC,c.created_at DESC,c.id DESC LIMIT 1
) latest ON true
WHERE (CAST(:cursor AS uuid) IS NULL OR cc.user_id>CAST(:cursor AS uuid))
AND (CAST(:allow_unbound AS boolean) OR cc.retention_compat_json IS NOT NULL
     OR latest.through_message_id IS NOT NULL)
AND NOT EXISTS (
  SELECT 1 FROM privacy_subject_barriers b WHERE b.user_id=cc.user_id AND b.state<>'active'
)
AND NOT EXISTS (
  SELECT 1 FROM async_jobs j WHERE j.user_id=cc.user_id
    AND j.job_type='conversation_checkpoint' AND j.state IN ('ready','running')
    AND j.payload->'memory_generation'=to_jsonb(cc.memory_generation)
)
AND (cc.retention_pressure_through_message_id IS NOT NULL
     OR latest.created_at IS NULL OR latest.created_at<=now()-interval '60 seconds')
AND (
  cc.retention_pressure_through_message_id IS NOT NULL OR
  (cc.pending_anchor_message_id IS NOT NULL AND (
    NOT EXISTS (
      SELECT 1 FROM messages pending_message WHERE pending_message.user_id=cc.user_id
        AND pending_message.id>COALESCE(latest.through_message_id,CASE WHEN CAST(:allow_unbound AS boolean) THEN 0 ELSE (cc.retention_compat_json->>'source_after')::bigint END,0)
        AND pending_message.id<cc.pending_anchor_message_id
    )
    OR COALESCE((
      SELECT sum(COALESCE(CASE WHEN pending_message.content_ciphertext IS NULL THEN octet_length(pending_message.content) ELSE octet_length(pending_message.content_ciphertext)-32 END,0)+24)
      FROM messages pending_message WHERE pending_message.user_id=cc.user_id
        AND pending_message.id>COALESCE(latest.through_message_id,CASE WHEN CAST(:allow_unbound AS boolean) THEN 0 ELSE (cc.retention_compat_json->>'source_after')::bigint END,0)
        AND pending_message.id<cc.pending_anchor_message_id
    ),0)>=1024
  ))
  OR EXISTS (
    SELECT 1 FROM messages m
    WHERE m.user_id=cc.user_id AND m.id>COALESCE(latest.through_message_id,CASE WHEN CAST(:allow_unbound AS boolean) THEN 0 ELSE (cc.retention_compat_json->>'source_after')::bigint END,0)
    GROUP BY m.user_id HAVING sum(COALESCE(CASE WHEN m.content_ciphertext IS NULL THEN octet_length(m.content) ELSE octet_length(m.content_ciphertext)-32 END,0)+24)>=4096
  )
)
ORDER BY cc.user_id
LIMIT :limit
""")
_RETENTION_RESUME_WRAP = text(str(_RETENTION_RESUME_USERS).replace(
    "(CAST(:cursor AS uuid) IS NULL OR cc.user_id>CAST(:cursor AS uuid))",
    "cc.user_id<=CAST(:cursor AS uuid)"))

_RETENTION_CURRENT = text("""
SELECT cc.memory_generation,cc.pending_anchor_message_id,cc.pending_plan_revision,
       cc.anchor_revision,(SELECT max(m.id) FROM messages m WHERE m.user_id=cc.user_id),
       cc.retention_pressure_through_message_id
FROM chat_contexts cc JOIN profiles p ON p.id=cc.user_id
WHERE cc.user_id=:user_id AND cc.memory_generation=:generation
  AND NOT EXISTS (SELECT 1 FROM privacy_subject_barriers b
                  WHERE b.user_id=cc.user_id AND b.state<>'active')
  AND NOT EXISTS (
    SELECT 1 FROM async_jobs j WHERE j.user_id=cc.user_id
      AND j.job_type='conversation_checkpoint' AND j.state IN ('ready','running')
      AND j.payload->'memory_generation'=to_jsonb(cc.memory_generation)
  )
""")

_CLEAR_RETENTION_TARGET = text("""
UPDATE chat_contexts cc
SET pending_anchor_message_id=NULL,pending_plan_revision=NULL
WHERE cc.user_id=:user_id AND cc.memory_generation=:generation
  AND cc.pending_anchor_message_id=:target
  AND cc.pending_plan_revision IS NOT DISTINCT FROM CAST(:plan_revision AS bigint)
  AND cc.anchor_revision=:anchor_revision
  AND NOT EXISTS (
    SELECT 1 FROM messages m WHERE m.user_id=cc.user_id
      AND m.id>:through_id AND m.id<cc.pending_anchor_message_id
  )
RETURNING user_id
""")

_RETENTION_DEAD_PLAN = text("""
WITH RECURSIVE replay_chain AS (
  SELECT j.id,j.state,j.created_at
  FROM async_jobs j JOIN chat_contexts cc ON cc.user_id=j.user_id
  WHERE j.user_id=:user_id AND j.job_type='conversation_checkpoint'
    AND j.dedup_key=:dedup_key AND j.state='dead'
    AND j.payload->'memory_generation'=to_jsonb(cc.memory_generation)
    AND cc.memory_generation=:generation
    AND COALESCE(j.payload->>'mode','normal')='normal'
    AND NOT EXISTS (
      SELECT 1 FROM async_jobs active_job WHERE active_job.user_id=cc.user_id
        AND active_job.job_type='conversation_checkpoint' AND active_job.state IN ('ready','running')
        AND active_job.payload->'memory_generation'=to_jsonb(cc.memory_generation)
    )
    AND NOT EXISTS (
      SELECT 1 FROM conversation_checkpoints c
      WHERE c.user_id=cc.user_id AND c.memory_generation=cc.memory_generation
        AND c.version='conversation-checkpoint-summary-v5' AND c.publish_state='published'
        AND (c.through_message_id>=:through_id
             OR (NOT :pressure AND c.created_at>now()-interval '60 seconds'))
    )
    AND COALESCE((
      SELECT c.id::text FROM conversation_checkpoints c
      WHERE c.user_id=cc.user_id AND c.memory_generation=cc.memory_generation
        AND c.version='conversation-checkpoint-summary-v5' AND c.publish_state='published'
      ORDER BY c.through_message_id DESC,c.created_at DESC,c.id DESC LIMIT 1
    ),'')=:previous_id
  UNION ALL
  SELECT child.id,child.state,child.created_at
  FROM async_jobs child JOIN replay_chain parent ON child.replay_of=parent.id
  WHERE child.user_id=:user_id AND child.job_type='conversation_checkpoint'
)
SELECT c.id FROM replay_chain c
WHERE c.state='dead'
  AND NOT EXISTS (SELECT 1 FROM replay_chain done WHERE done.state IN ('ready','running','succeeded'))
ORDER BY c.created_at DESC,c.id DESC LIMIT 1
""")

_RETENTION_UNPUBLISHED_SUCCESS = text("""
SELECT j.id,j.payload,j.max_attempts,j.attempt
FROM async_jobs j JOIN chat_contexts cc ON cc.user_id=j.user_id
WHERE j.user_id=:user_id AND j.job_type='conversation_checkpoint'
  AND cc.memory_generation=:generation
  AND j.payload->'memory_generation'=to_jsonb(cc.memory_generation)
  AND j.payload->>'summarizer_version'='conversation-checkpoint-summary-v5'
  AND COALESCE(j.payload->>'mode','normal') IN ('normal','publish_retention')
  AND j.payload->>'source_hash'=:source_hash
  AND j.payload->'through_message_id'=to_jsonb(CAST(:through_id AS bigint))
  AND COALESCE(j.payload->>'previous_checkpoint_id','')=:previous_id
  AND j.state='succeeded'
  AND (j.result_code IN ('checkpoint_disabled','source_changed','checkpoint_progress_page','ok')
       OR (j.result_code='checkpoint_draft_ready' AND j.payload->>'mode'='publish_retention'))
  AND j.payload_redacted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM privacy_subject_barriers b
                  WHERE b.user_id=cc.user_id AND b.state<>'active')
  AND NOT EXISTS (SELECT 1 FROM async_jobs active_job
    WHERE active_job.user_id=cc.user_id AND active_job.job_type='conversation_checkpoint'
      AND active_job.state IN ('ready','running')
      AND active_job.payload->'memory_generation'=to_jsonb(cc.memory_generation))
  AND NOT EXISTS (SELECT 1 FROM conversation_checkpoints c
    WHERE c.user_id=cc.user_id AND c.memory_generation=cc.memory_generation
      AND c.version='conversation-checkpoint-summary-v5' AND c.publish_state='published'
      AND c.through_message_id>=:through_id)
  AND COALESCE((SELECT c.id::text FROM conversation_checkpoints c
    WHERE c.user_id=cc.user_id AND c.memory_generation=cc.memory_generation
      AND c.version='conversation-checkpoint-summary-v5' AND c.publish_state='published'
    ORDER BY c.through_message_id DESC,c.created_at DESC,c.id DESC LIMIT 1),'')=:previous_id
  -- 실제 실패로 끝난 최신 작업의 재시도 예산을 새 성공 재개로 우회하지 않는다.
  AND NOT EXISTS (SELECT 1 FROM async_jobs failed
    WHERE failed.user_id=j.user_id AND failed.job_type=j.job_type AND failed.state='dead'
      AND failed.payload->'memory_generation'=j.payload->'memory_generation'
      AND failed.payload->>'source_hash'=:source_hash
      AND COALESCE(failed.payload->>'mode','normal') IN ('normal','publish_retention')
      AND (failed.finished_at,failed.id)>(j.finished_at,j.id))
ORDER BY (j.payload->>'mode'='publish_retention') DESC NULLS LAST,
         COALESCE((j.payload->>'progress_next_chunk')::bigint,0) DESC,
         j.finished_at DESC,j.id DESC LIMIT 1
""")


# 같은 계획(source_hash)의 마지막 종료 작업. 원본·다음 쪽·다시 만든 작업을 모두 본다.
_RETENTION_PLAN_LATEST_TERMINAL = text("""
SELECT j.state,j.id,j.finished_at,j.last_error_code,j.payload->>'redo_depth'
FROM async_jobs j JOIN chat_contexts cc ON cc.user_id=j.user_id
WHERE j.user_id=:user_id AND j.job_type='conversation_checkpoint'
  AND cc.memory_generation=:generation
  AND j.payload->'memory_generation'=to_jsonb(cc.memory_generation)
  AND j.payload->>'summarizer_version'='conversation-checkpoint-summary-v5'
  AND j.payload->>'source_hash'=:source_hash
  AND COALESCE(j.payload->>'mode','normal') IN ('normal','publish_retention')
  AND j.state IN ('succeeded','cancelled','dead')
ORDER BY j.finished_at DESC,j.id DESC LIMIT 1
""")

# 같은 계획이 dead로 막혔을 때 다시 만드는 오류 코드. 원문 범위가 같으면 다음 턴에도 같은 계획이
# 나와 dedup_key가 막혀 있으므로, 다시 만들지 않으면 그 사용자의 요약 게시가 영구히 멈춘다.
# 다시 만드는 횟수는 jobs.REDO_MAX_DEPTH로 묶인다(같은 입력에서 늘 실패해도 모델 호출이 끝난다).
_CHECKPOINT_REDO_CODES = frozenset({
    # 모델 출력의 내용 실패 — 표본마다 결과가 달라 다시 하면 풀릴 수 있다.
    "summary_invalid", "summary_name_leak", "summary_length", "summary_packing_length_snapshot",
    "checkpoint_packing_attempts_exhausted",
    # 일시 장애 — 모델 호출 실패, 소비자(worker/consumer.py)·회수 작업(jobs reaper)이 남기는 코드.
    "summary_llm_failed", "unexpected_error", "timeout",
    jobs.ERROR_LEASE_EXPIRED, jobs.ERROR_ATTEMPTS_EXHAUSTED,
})
# 일부러 다시 만들지 않는 dead 코드와 이유. 요약 처리기(worker/checkpoint_jobs.py)에 새 JobRetry·
# JobFatal 코드를 추가하면 위 목록이나 여기 중 한쪽에 넣어야 한다(tests/test_checkpoint_redo_and_reason.py).
_CHECKPOINT_NO_REDO_CODES = {
    "invalid_payload": "payload 형식 오류 — 같은 계획으로 다시 만들어도 같다",
    "unsupported_payload_schema": "payload 형식 오류 — 같은 계획으로 다시 만들어도 같다",
    "invalid_baseline_binding": "payload 형식 오류 — 같은 계획으로 다시 만들어도 같다",
    "missing_user": "payload 형식 오류 — 사용자 없는 작업",
    "invalid_reverify_payload": "payload 형식 오류(재검증) — 재검증 작업은 계획 계보에 없다",
    "checkpoint_packing_version": "payload 형식 오류 — 알 수 없는 묶음 형식",
    "checkpoint_packing_cap": "payload 형식 오류 — 묶음 상한 값",
    "checkpoint_packing_attempts": "payload 형식 오류 — 남은 호출 수 값",
    "checkpoint_packing_control_invalid": "payload 형식 오류 — 제어값 외 변경",
    "checkpoint_packing_mode": "payload 형식 오류 — 모드와 묶음 형식 불일치",
    "checkpoint_packing_state": "payload 형식 오류 — 이어 쓸 상태가 검증되지 않음",
    "checkpoint_packing_multiple_calls": "코드 계약 위반 — 한 시도에 모델 호출 두 번",
    "preparation_requires_retention_checkpoint": "배포 준비 경로의 형식 오류 — 같은 계획으로 다시 만들어도 같다",
    "checkpoint_completed_proof_invalid": "완성 초안이 지금 원문과 맞지 않는 결정적 실패 — 원문이 바뀌면 새 계획이 생긴다",
    "reverify_invalid": "재검증 작업 — 계획 계보(normal·publish_retention)에 없어 여기서 보지 않는다",
    "unknown_job_type": "배포 불일치(처리기 없음) — 다시 만들어도 같다",
}


async def _latest_plan_terminal(session, *, user_id, generation, plan):
    return (await retention_release.execute(session, _RETENTION_PLAN_LATEST_TERMINAL, {
        "user_id": user_id, "generation": generation, "source_hash": plan.source_hash,
    })).first()


async def _redo_dead_checkpoint(session, *, user_id, plan, row):
    """같은 계획의 마지막 종료 작업(row)이 dead면 간격(30분·1시간·2시간·4시간)을 두고 새로 만든다.

    dead 행을 되살리거나 payload를 복제하지 않는다. 지금의 계획으로 새 행을 만들고,
    같은 dead마다 한 건만 생긴다(dedup_key `checkpoint-redo:{dead id}`).
    반환 = (새 작업 id 또는 None, 상한에 닿아 멈췄는지). 상한에 닿으면 더 만들지 않는다.
    그동안 대화는 요약 없이 줄인 최근 기록으로 계속된다.
    """
    if row is None:
        return None, False
    redo = jobs.dead_redo(row, now=datetime.now(timezone.utc), repeat_codes=_CHECKPOINT_REDO_CODES)
    if redo is None:
        return None, False
    if redo.exhausted:
        # 식별자·코드·깊이만 남긴다(원문 없음).
        _log.warning("checkpoint_redo_exhausted dead_job=%s error_code=%s redo_depth=%d",
                     row[1], row[3], redo.depth - 1)
        return None, True
    made = await checkpoint_repo.enqueue_checkpoint(session, user_id=user_id, plan=plan,
                                                    redo=(str(row[1]), redo.depth))
    if made is not None:
        _log.info("checkpoint_redo_enqueued dead_job=%s error_code=%s redo_depth=%d",
                  row[1], row[3], redo.depth)
    return made, False


async def _resume_unpublished_success(session, *, user_id, generation, plan, previous):
    """실제 게시가 없는 동일 계획의 마지막 durable payload만 새 행으로 이어 간다."""
    row = (await retention_release.execute(session, _RETENTION_UNPUBLISHED_SUCCESS, {
        "user_id": user_id, "generation": generation, "source_hash": plan.source_hash,
        "through_id": plan.through_message_id, "previous_id": str(previous.id) if previous else "",
    })).first()
    if row is None:
        return None
    payload = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    if not await context_retention.privacy_epoch_matches(session, user_id=user_id,
                                                        privacy_epoch=payload.get("privacy_epoch")):
        return None
    if not await checkpoint_repo.compat_binding_matches(session, user_id=user_id,
            generation=generation, binding=payload.get("baseline_binding")):
        return None
    # SQL의 상태 조건과 별개로 원본 범위/부모 계약을 모두 비교한다.
    if any(payload.get(key) != value for key, value in plan.payload().items() if key != "reason"):
        raise context_retention.RetentionError("checkpoint_resume_plan_changed")
    # Completed proof binds the original reason as well as source/epoch.
    # Reuse a publish-only draft verbatim; do not turn it into a model page.
    if plan.reason == "input_pressure" and payload.get("mode") != "publish_retention":
        payload = {**payload, "reason": "input_pressure"}
    if "progress_state" in payload:
        if (type(payload.get("progress_next_chunk")) is not int
                or payload["progress_next_chunk"]<=0
                or not isinstance(payload.get("progress_manifest"), str)):
            raise context_retention.RetentionError("checkpoint_resume_progress")
        await context_retention.validate_progress_sources(session, user_id=user_id,
            state=payload["progress_state"])
        checkpoint_repo.validate_compat_state(payload["progress_state"], payload.get("baseline_binding"))
    return await jobs.enqueue(session, queue=jobs.QUEUE_CONTENT,
        job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT, user_id=user_id,
        dedup_key=f"checkpoint-resume:{row[0]}", payload=payload,
        max_attempts=max(1, min(int(row[2]), int(row[2])-int(row[3])+1)) if len(row)>=4 else 3)


@dataclass(frozen=True, slots=True)
class RetentionResumeResult:
    candidates: int = 0
    enqueued: int = 0
    replayed: int = 0
    busy: int = 0
    blocked: int = 0


async def _retention_enabled(session, *, lock: bool = False) -> bool:
    return await context_retention.enabled_now(session, lock=lock)


_RESUME_CURSOR_READ = text("SELECT value FROM app_config WHERE key='context_retention_resume_cursor'")
_RESUME_CURSOR_WRITE = text("""
INSERT INTO app_config(key,value)
SELECT 'context_retention_resume_cursor',CAST(:next AS jsonb)
WHERE CAST(:expected AS jsonb)='null'::jsonb OR EXISTS (
  SELECT 1 FROM app_config current_cursor
  WHERE current_cursor.key='context_retention_resume_cursor'
    AND current_cursor.value IS NOT DISTINCT FROM CAST(:expected AS jsonb)
)
ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
WHERE app_config.value IS NOT DISTINCT FROM CAST(:expected AS jsonb)
RETURNING key
""")


async def _read_resume_cursor(session) -> str | None:
    value = await session.scalar(_RESUME_CURSOR_READ)
    if value is None:
        return None
    if not isinstance(value, str):
        raise context_retention.RetentionError("checkpoint_resume_cursor")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise context_retention.RetentionError("checkpoint_resume_cursor") from exc


async def _advance_resume_cursor(*, expected: str | None, visited: uuid.UUID) -> bool:
    # 사용자와 작업의 트랜잭션이 끝난 뒤에만 호출한다.
    async with get_sessionmaker()() as session:
        made = (await session.execute(_RESUME_CURSOR_WRITE, {
            "expected": json.dumps(expected), "next": json.dumps(str(visited)),
        })).first() is not None
        await session.commit()
        return made


async def _load_retention_segment(session, user_id, *, after_id: int, through_id: int):
    messages = []
    cursor = after_id
    for _ in range(RETENTION_SOURCE_MAX_PAGES):
        page = await checkpoint_repo.load_range(
            session, user_id, after_id=cursor, through_id=through_id,
            max_rows=RETENTION_SOURCE_PAGE_SIZE, include_excluded=True,
        )
        if not page:
            break
        # 잘못된/중복 페이지에서 원문을 건너뛰거나 무한 순회하지 않는다.
        if any(m.id<=cursor or m.id>through_id for m in page) or any(
            left.id>=right.id for left,right in zip(page,page[1:])
        ):
            raise context_retention.RetentionError("checkpoint_sweep_source_order")
        messages.extend(page)
        cursor = page[-1].id
        if (cursor>=through_id or len(page)<RETENTION_SOURCE_PAGE_SIZE
                or context_retention.source_tokens(messages)>=RETENTION_SOURCE_BYTE_BUDGET):
            break
    return messages


async def _resume_retention_checkpoints() -> RetentionResumeResult:
    # A sweep is a new producer, even when its parent job predates activation.
    # Pin its own global release once; never rebind an already-running job.
    from app.services.retention_preparation_context import without_binding
    with without_binding():
        return await _resume_retention_checkpoints_bound()


async def _resume_retention_checkpoints_bound() -> RetentionResumeResult:
    """job finalize 밖에서 사용자별 짧은 예약 트랜잭션을 실행한다.

    예약만 멱등으로 수행한다. 모델·요약 게시·원문 압축은 하지 않는다. sweep의 job 행 잠금을
    가진 apply_domain 안에서 호출하면 job→user 잠금 순서가 되므로 여기서는 별도 세션을 쓴다.
    """
    try:
        async with get_sessionmaker()() as session:
            from app.services.retention_preparation_context import set_binding
            set_binding(await retention_release.resolve(session))
            if not await _retention_enabled(session):
                return RetentionResumeResult()
            cursor = await _read_resume_cursor(session)
            allow_unbound = retention_release.binding() is not None
            candidates = list((await retention_release.execute(session, _RETENTION_RESUME_USERS,
                {"limit": SWEEP_LIMIT, "cursor": cursor, "allow_unbound": allow_unbound})).all())
            if cursor is not None and len(candidates)<SWEEP_LIMIT:
                candidates.extend((await retention_release.execute(session, _RETENTION_RESUME_WRAP,
                    {"limit": SWEEP_LIMIT-len(candidates), "cursor": cursor,
                     "allow_unbound": allow_unbound})).all())
    except Exception as exc:
        _log.warning("후속 요약 예약 점검 실패 error=%s", type(exc).__name__)
        return RetentionResumeResult(blocked=1)

    enqueued = replayed = busy = blocked = 0
    owns_cursor = True
    for user_id, generation in candidates:
        try:
            async with get_sessionmaker()() as session:
                # 아무 사용자/잡 행 쓰기 전에 시도한다. 바쁘면 기다리지 않고 다음 sweep에 맡긴다.
                locked = await session.scalar(_TRY_DOCUMENT_USER_LOCK, {"user_id": str(user_id)})
                if not locked:
                    busy += 1
                    continue
                if not await _retention_enabled(session, lock=True):
                    break
                if not await context_retention.adopted_user(session, user_id=user_id, lock=True):
                    continue
                state = (await retention_release.execute(session, _RETENTION_CURRENT, {
                    "user_id": user_id, "generation": int(generation),
                })).first()
                if state is None:
                    continue
                previous = await checkpoint_repo.load_latest(session, user_id, retention_enabled=True)
                baseline = await checkpoint_repo.load_compat_baseline(session,
                    user_id=user_id, generation=int(generation))
                if baseline is None and previous is None and retention_release.binding() is None:
                    # A background sweep may not enroll historical users. The
                    # first ordinary request captures its own immutable suffix.
                    continue
                through = previous.through_message_id if previous else baseline.source_after if baseline else 0
                target, latest_id = state[1], state[4]
                pressure_target = state[5] if len(state)>5 else None
                if pressure_target is not None and through>=pressure_target:
                    if await context_retention.processing_complete(session, user_id=user_id,
                            generation=int(generation), through_id=through):
                        await checkpoint_repo.clear_completed_pressure(session, user_id=user_id,
                            generation=int(generation), through_id=through)
                        pressure_target = None
                if target is not None:
                    cleared = (await session.execute(_CLEAR_RETENTION_TARGET, {
                        "user_id": user_id, "generation": int(generation), "target": int(target),
                        "plan_revision": state[2], "anchor_revision": int(state[3]), "through_id": through,
                    })).first()
                    if cleared is not None:
                        target = None
                if latest_id is None or int(latest_id)<=through:
                    await session.commit()
                    continue
                if pressure_target is not None:
                    messages = await checkpoint_repo.load_pressure_sources(session, user_id,
                        after_id=through, through_id=int(pressure_target))
                else:
                    messages = await _load_retention_segment(
                        session, user_id, after_id=through, through_id=int(latest_id))
                if not messages:
                    await session.commit()
                    continue
                tail = checkpoint.keep_tail(messages, retention_enabled=True)
                keep_from = tail[0].id
                # 충분한 새 원문이 있으면 현재 tail로 목표를 확장한다. 작은 옛 목표에 계속
                # 묶이면 최소 신규 구간을 영원히 못 채워 다른 발화 후에도 복구되지 않는다.
                if target is not None and not checkpoint.should_checkpoint(messages, retention_enabled=True):
                    keep_from = int(target)
                reset_triggered = target is not None
                plan = checkpoint.plan(
                    messages, previous=previous, keep_from_message_id=keep_from,
                    memory_generation=int(generation), reset_triggered=reset_triggered,
                    version=checkpoint.RETENTION_SUMMARIZER_VERSION,
                    through_message_id=messages[-1].id if pressure_target is not None else None,
                    reason="input_pressure" if pressure_target is not None else None,
                    baseline_binding=baseline.payload() if baseline else None,
                    **({"preparation_id": retention_release.binding().preparation_id}
                       if retention_release.binding() is not None else {}),
                )
                if plan is None:
                    # 최신 한 턴이 tail 전체를 차지해도 처리를 시작할 수 있다.
                    pressure_job = None
                    if checkpoint.should_checkpoint(messages, retention_enabled=True):
                        pressure = await checkpoint_repo.request_pressure(session, user_id=user_id,
                            generation=int(generation))
                        pressure_job = pressure.job_id
                    await session.commit()
                    enqueued += int(pressure_job is not None)
                    continue
                made = await checkpoint_repo.maybe_enqueue(
                    session, user_id=user_id, messages=messages, keep_from_message_id=keep_from,
                    reset_triggered=reset_triggered, retention_enabled=True,
                    **({"pressure_through_message_id": plan.through_message_id}
                       if pressure_target is not None else {}),
                )
                exhausted = False
                if made is None:
                    # Exhausted attempts are not a fresh retry allowance every sweep.
                    # Preserve the target and old job. A dead plan is only re-created
                    # as a new row after its widening interval (_redo_dead_checkpoint).
                    dead = (await retention_release.execute(session, _RETENTION_DEAD_PLAN, {
                        "user_id": user_id, "generation": int(generation),
                        "dedup_key": retention_release.dedup_key(plan.dedup_key(user_id)), "through_id": plan.through_message_id,
                        "previous_id": str(previous.id) if previous else "",
                        "pressure": pressure_target is not None,
                    })).first()
                    latest = await _latest_plan_terminal(session, user_id=user_id,
                        generation=int(generation), plan=plan)
                    if dead is None or (latest is not None and latest[0] != "dead"):
                        # 원본 dead 뒤에 다시 만든 작업이 성공으로 끝났으면 그 상태에서 이어받는다.
                        made = await _resume_unpublished_success(session, user_id=user_id,
                            generation=int(generation), plan=plan, previous=previous)
                    if made is None:
                        # 원본이 dead이거나, 다음 쪽 작업이 dead라 이어받기가 막힌 경우.
                        made, capped = await _redo_dead_checkpoint(session, user_id=user_id,
                                                                   plan=plan, row=latest)
                        exhausted = made is None and (dead is not None or capped)
                await session.commit()
                enqueued += int(made is not None)
                blocked += int(exhausted)
        except Exception as exc:
            blocked += 1
            # 원문·SQL·접속정보가 섞인 예외 문자열은 남기지 않는다.
            _log.warning("후속 요약 예약 보류 error=%s", type(exc).__name__)
        finally:
            # busy/실패도 방문으로 센다. 이전 배치가 새 커서를 덮어쓰지 않는다.
            if owns_cursor:
                try:
                    owns_cursor = await _advance_resume_cursor(expected=cursor, visited=user_id)
                    if owns_cursor:
                        cursor = str(user_id)
                except Exception as exc:
                    owns_cursor = False
                    _log.warning("후속 요약 순환 위치 기록 실패 error=%s", type(exc).__name__)
    return RetentionResumeResult(len(candidates), enqueued, replayed, busy, blocked)

# ── 정지 감지의 기준은 **사용자 커서**다 ────────────────────────────────
#
# 예전에는 "죽은 잡"만 봤다. 그런데 실제 정지는 대부분 잡이 **성공**했는데 다음 잡이 안 생긴
# 경우다(키 충돌·enqueue 실패). 그때는 죽은 잡 행 자체가 없어 감지 밖이었다. 취소된 잡,
# 애초에 잡이 하나도 없는 경우도 마찬가지였다.
#
# 믿을 수 있는 신호는 하나다: **커서가 뒤처졌는데 대기·실행 중인 잡이 없다.**
# ⚠️ 기다린 시간은 **처리 안 된 가장 오래된 턴의 나이**로 잰다. 상태 행의 `updated_at`으로 재면
#    안 된다 — 그 값은 대화 커서 전진 때문에 **매 턴 새로 찍히므로**, 사슬이 멈춘 사람이 계속
#    대화하는 한 유예 조건을 영원히 못 넘는다. 그러면 대화하는 내내 기억이 안 쌓이는데
#    감지도 안 된다. 이번 수정이 잡으려던 사고가 정확히 그 모양이었다.
_STALLED_USERS = text("""
SELECT s.user_id, s.ingest_through_turn_seq AS cursor, s.repair_generation, s.privacy_epoch,
       nxt.turn_seq AS next_turn
FROM memory_pipeline_states s
JOIN LATERAL (
  SELECT MIN(m.turn_seq) AS turn_seq
  FROM messages m
  WHERE m.user_id = s.user_id AND m.kind = 'normal' AND m.turn_seq IS NOT NULL
    AND m.turn_seq > s.ingest_through_turn_seq
    AND m.turn_seq <= s.source_through_turn_seq
    AND m.created_at < now() - make_interval(mins => :grace)
) nxt ON nxt.turn_seq IS NOT NULL
WHERE s.mode <> 'legacy'
  AND s.bootstrap_status = 'ready'
  AND s.ingest_through_turn_seq < s.source_through_turn_seq
  AND NOT EXISTS (
    SELECT 1 FROM async_jobs j
    WHERE j.user_id = s.user_id AND j.job_type = 'mem0_ingest'
      AND j.state IN ('ready', 'running')
  )
ORDER BY nxt.turn_seq
LIMIT :limit
""")

_LOCK_STALLED_PIPELINE = text("""
SELECT user_id FROM memory_pipeline_states
WHERE user_id=:user_id AND ingest_through_turn_seq=:cursor
  AND repair_generation=:generation AND privacy_epoch=:epoch
  AND mode<>'legacy' AND bootstrap_status='ready'
  AND source_through_turn_seq>ingest_through_turn_seq
FOR UPDATE SKIP LOCKED
""")
_STALLED_STILL_MISSING = text("""
SELECT NOT EXISTS (SELECT 1 FROM async_jobs WHERE user_id=:user_id
  AND job_type='mem0_ingest' AND state IN ('ready','running'))
AND :next_turn=(SELECT MIN(m.turn_seq) FROM messages m
  JOIN memory_pipeline_states s ON s.user_id=m.user_id
  WHERE m.user_id=:user_id AND m.kind='normal' AND m.turn_seq>:cursor
    AND m.turn_seq<=s.source_through_turn_seq)
""")


async def _current_stalled(session, candidates):
    """Lock first, then use a fresh statement snapshot to recheck competing producers."""
    current = []
    for user_id, cursor, generation, epoch, next_turn in candidates:
        if next_turn is None:
            continue
        params = dict(user_id=user_id, cursor=int(cursor), generation=int(generation),
                      epoch=int(epoch), next_turn=int(next_turn))
        if (await session.execute(_LOCK_STALLED_PIPELINE, params)).first() is None:
            continue
        if await session.scalar(_STALLED_STILL_MISSING, params):
            current.append((user_id, cursor, generation, epoch, next_turn))
    return current

# 판정만 죽은 경우. 추출 커서는 계속 따라잡아 결국 `ingest == source`가 되므로 위 조건에
# 안 걸린다. 그러면 그 기억은 영원히 미판정으로 남아 회상에 안 나오고 전환 관문도 막는다.
_UNJUDGED_USERS = text("""
SELECT r.user_id, MIN(r.source_turn_seq) AS turn_seq,
       MAX(s.repair_generation) AS repair_generation, MAX(s.privacy_epoch) AS privacy_epoch
FROM mem0_memory_registry r
JOIN memory_pipeline_states s ON s.user_id = r.user_id
WHERE r.semantic_status = 'pending'
  AND s.mode <> 'legacy'
  AND r.created_at < now() - make_interval(mins => :grace)
  AND NOT EXISTS (
    SELECT 1 FROM async_jobs j
    WHERE j.user_id = r.user_id AND j.job_type = 'mem0_consolidate'
      AND j.state IN ('ready', 'running')
  )
GROUP BY r.user_id
ORDER BY 2
LIMIT :limit
""")

# 죽은 채로 아직 replay되지 않은 기억 잡. 커서가 뒤처진 사용자로 한정한다 — 뒤처지지 않았다면
# 그 잡이 죽었어도 체인은 이미 지나갔으므로 되살릴 이유가 없다.
#
# 위 두 질의가 주 감지 수단이고, 이건 보조다.
_STALLED = text("""
SELECT j.id, j.user_id
FROM async_jobs j
JOIN memory_pipeline_states s ON s.user_id = j.user_id
WHERE j.state = 'dead'
  AND j.queue = 'memory'
  AND s.mode <> 'legacy'
  AND s.bootstrap_status = 'ready'
  AND s.ingest_through_turn_seq < s.source_through_turn_seq
  AND NOT EXISTS (
    SELECT 1 FROM async_jobs r
    WHERE r.replay_of = j.id AND r.state IN ('ready', 'running', 'succeeded')
  )
ORDER BY j.created_at
LIMIT :limit
""")


# 임베딩이 비어 있는 일기 회상 문서.
#
# 여기도 같은 문제가 있다. 임베딩 잡이 죽으면 그 일기는 아무도 다시 걸어주지 않아 영영
# 검색 대상에서 빠진다. 오류도 안 나고 문자열 부분일치로는 걸리기 때문에 눈에 안 띈다.
#
# `embedding_repair_attempts`는 DB에서 0~3으로 제한돼 있는데, 그동안 이 값을 **올리는 코드가
# 없었다.** 그래서 상한이 아무 일도 하지 않았다. 여기서 올린다.
MAX_EMBEDDING_REPAIR = 3

# A valid pending shared row has its own owner-independent repair budget. In
# particular, ready shared rows with NULL private embeddings are healthy.
_VALID_SHARED_DOCUMENT = """
  SELECT 1 FROM diary_template_embeddings se
  WHERE se.id=rd.shared_embedding_id
    AND se.source_hash=rd.source_hash AND se.embedding_model=rd.embedding_model
    AND se.index_version=rd.index_version AND se.dimensions=1536
    AND se.search_text=rd.search_text
    AND EXISTS (SELECT 1 FROM diaries owned_diary
                WHERE owned_diary.user_id=rd.user_id AND owned_diary.id=rd.diary_id
                  AND owned_diary.kind IN ('welcome','capi_day'))
"""

_MISSING_EMBEDDING = text(f"""
SELECT rd.user_id, rd.diary_id, rd.embedding_repair_attempts
FROM diary_recall_documents rd
JOIN diaries d ON d.user_id=rd.user_id AND d.id=rd.diary_id
WHERE rd.embedding IS NULL
  AND rd.embedding_repair_attempts < :max_attempts
  AND d.record_status='published' AND d.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM privacy_subject_barriers b
                  WHERE b.user_id=rd.user_id AND b.state <> 'active')
  AND NOT EXISTS ({_VALID_SHARED_DOCUMENT})
ORDER BY rd.updated_at, rd.user_id, rd.diary_id
LIMIT :limit
""")

_MISSING_SHARED_EMBEDDING = text("""
SELECT se.id, se.embedding_repair_attempts
FROM diary_template_embeddings se
WHERE se.embedding IS NULL AND se.embedding_repair_attempts < :max_attempts
  AND NOT EXISTS (
    SELECT 1 FROM async_jobs j
    WHERE j.job_type='diary_template_embed' AND j.user_id IS NULL
      AND j.payload->>'shared_id'=se.id::text AND j.state IN ('ready','running')
  )
ORDER BY se.updated_at, se.id
LIMIT :limit
""")

_BUMP_SHARED_REPAIR_BATCH = text("""
UPDATE diary_template_embeddings se
SET embedding_repair_attempts=se.embedding_repair_attempts+1,updated_at=now()
FROM unnest(:shared_ids ::uuid[], :repair_attempts ::integer[]) AS t(id,attempt)
WHERE se.id=t.id AND se.embedding IS NULL
  AND se.embedding_repair_attempts=t.attempt
  AND se.embedding_repair_attempts < :max_attempts
  AND NOT EXISTS (
    SELECT 1 FROM async_jobs j
    WHERE j.job_type='diary_template_embed' AND j.user_id IS NULL
      AND j.payload->>'shared_id'=se.id::text AND j.state IN ('ready','running')
  )
RETURNING se.id,se.embedding_repair_attempts
""")


async def _enqueue_shared_repairs(session, missing: list) -> int:
    if not missing:
        return 0
    bumped = (await session.execute(
        _BUMP_SHARED_REPAIR_BATCH,
        {
            "shared_ids": [shared_id for shared_id, _attempt in missing],
            "repair_attempts": [int(attempt) for _shared_id, attempt in missing],
            "max_attempts": MAX_EMBEDDING_REPAIR,
        },
    )).all()
    return await jobs.enqueue_many(
        session,
        queue=jobs.QUEUE_CONTENT,
        job_type=diary_shared_embeddings.JOB_DIARY_TEMPLATE_EMBED,
        items=[
            (
                None,
                f"diarytemplate:repair:{shared_id}:{attempt}",
                {"shared_id": str(shared_id), "schema_version": diary_recall_repo.INDEX_VERSION},
            )
            for shared_id, attempt in bumped
        ],
    )


# 발행됐는데 **회상 문서 자체가 없는** 일기.
#
# 임베딩이 비면 위 질의가 잡지만, 문서가 통째로 없으면 잡을 행이 없어서 영영 안 걸린다.
# 그 비대칭이 결함이다. 실제로 dev에서 일기 한 건이 색인에 없는 채로 남아 있었다.
#
# ⚠️ 조건은 `diary_recall_repo`의 upsert와 **똑같아야 한다.** 느슨하게 잡으면 삭제가 진행 중인
# 사용자의 일기를 되살린다.
_MISSING_DOCUMENT = text("""
SELECT d.user_id, d.id AS diary_id
FROM diaries d
WHERE d.record_status='published'
  AND d.deleted_at IS NULL
  AND d.published_at IS NOT NULL
  AND d.kind IN ('welcome','shared_day','capi_day')
  AND NOT EXISTS (
    SELECT 1 FROM diary_recall_documents rd
    WHERE rd.user_id=d.user_id AND rd.diary_id=d.id
  )
  AND NOT EXISTS (
    SELECT 1 FROM privacy_subject_barriers b
    WHERE b.user_id=d.user_id AND b.state <> 'active'
  )
ORDER BY d.published_at DESC
LIMIT :limit
""")

# 올리면서 다시 확인한다. 그 사이 임베딩이 채워졌으면 건드리지 않는다.
# 배치(#15a): 대상 전체를 한 UPDATE로 올리고, 실제로 오른 행만 RETURNING으로 받는다.
_BUMP_REPAIR_BATCH = text(f"""
UPDATE diary_recall_documents rd
SET embedding_repair_attempts=rd.embedding_repair_attempts+1,updated_at=now()
FROM unnest(:user_ids ::uuid[], :diary_ids ::uuid[], :repair_attempts ::integer[])
     AS t(user_id,diary_id,attempt)
WHERE rd.user_id=t.user_id AND rd.diary_id=t.diary_id AND rd.embedding IS NULL
  AND rd.embedding_repair_attempts=t.attempt
  AND rd.embedding_repair_attempts < :max_attempts
  AND NOT EXISTS ({_VALID_SHARED_DOCUMENT})
  AND EXISTS (
    SELECT 1 FROM diaries d WHERE d.user_id=rd.user_id AND d.id=rd.diary_id
      AND d.record_status='published' AND d.deleted_at IS NULL
  )
  AND NOT EXISTS (SELECT 1 FROM privacy_subject_barriers b
                  WHERE b.user_id=rd.user_id AND b.state <> 'active')
RETURNING rd.user_id,rd.diary_id,rd.embedding_repair_attempts
""")

async def _enqueue_private_repairs(session, bumped: list) -> int:
    if not bumped:
        return 0
    items = [
        (
            user_id,
            f"diaryembed:repair:{diary_id}:{attempt}",
            {"schema_version": diary_recall_repo.INDEX_VERSION, "diary_id": str(diary_id)},
        )
        for user_id, diary_id, attempt in bumped
    ]
    # A source change resets its repair counter, so an old attempt's job can
    # still exist. Observe committed duplicates without waiting for finalize's
    # job-row UPDATE while this sweep already owns the user's advisory lock.
    existing = {
        row[0] for row in (await session.execute(text("""
            SELECT dedup_key FROM async_jobs
            WHERE job_type=:job_type AND dedup_key=ANY(CAST(:dedup_keys AS text[]))
        """), {"job_type": diary_recall_repo.JOB_DIARY_RECALL_EMBED,
               "dedup_keys": [key for _uid, key, _payload in items]})).all()
    }
    remaining = [item for item in items if item[1] not in existing]
    if not remaining:
        return 0
    # enqueue_many retains ON CONFLICT as the final guard for a new-job race.
    return await jobs.enqueue_many(
        session, queue=jobs.QUEUE_CONTENT,
        job_type=diary_recall_repo.JOB_DIARY_RECALL_EMBED, items=remaining,
    )


# #28: provider 삭제가 failed로 남은 잔존물 회수 — pending으로 되돌려 재시도 경로에 태운다.
# 노출은 semantic 필터가 이미 막고 있으므로(저장 비용 문제) bounded면 충분하다.
_RECOVER_FAILED_DELETES = text("""
WITH picked AS (
  SELECT id, user_id FROM mem0_memory_registry
  WHERE provider_delete_state = 'failed'
  ORDER BY updated_at
  LIMIT :limit
)
UPDATE mem0_memory_registry r
SET provider_delete_state = 'pending', updated_at = now()
FROM picked p WHERE r.id = p.id
RETURNING r.user_id
""")

_PIPELINE_EPOCHS = text("""
SELECT user_id, privacy_epoch FROM memory_pipeline_states WHERE user_id = ANY(:user_ids)
""")

_TRY_DOCUMENT_USER_LOCK = text(
    "SELECT pg_try_advisory_xact_lock(hashtextextended(:user_id, 0))"
)


async def _try_lock_document_users(session, undocumented: list) -> set:
    """Claim all later upsert locks before any row/job writes, never waiting.

    A repair can already hold an owned/canonical row by the time document
    creation runs. Acquiring its user lock then would invert publication's
    user -> row order. Busy users remain undocumented for the next sweep.
    """
    locked = set()
    for user_id in sorted({user_id for user_id, _diary_id in undocumented}, key=str):
        if await session.scalar(_TRY_DOCUMENT_USER_LOCK, {"user_id": str(user_id)}):
            locked.add(user_id)
    return locked


async def handle_memory_sweep(job: ClaimedJob) -> JobResult:
    """멈춘 사용자에게 다음 turn 잡을 다시 걸고, 비어 있는 일기 임베딩을 다시 만든다. 멱등이다."""
    async with get_sessionmaker()() as session:
        stalled = (await session.execute(
            _STALLED_USERS, {"limit": SWEEP_LIMIT, "grace": STALL_GRACE_MIN}
        )).all()
        unjudged = (await session.execute(
            _UNJUDGED_USERS, {"limit": SWEEP_LIMIT, "grace": STALL_GRACE_MIN}
        )).all()
        rows = (await session.execute(_STALLED, {"limit": SWEEP_LIMIT})).all()
        missing = (
            await session.execute(
                _MISSING_EMBEDDING,
                {"limit": SWEEP_LIMIT, "max_attempts": MAX_EMBEDDING_REPAIR},
            )
        ).all()
        shared_missing = (await session.execute(
            _MISSING_SHARED_EMBEDDING,
            {"limit": SWEEP_LIMIT, "max_attempts": MAX_EMBEDDING_REPAIR},
        )).all()
        undocumented = (
            await session.execute(_MISSING_DOCUMENT, {"limit": SWEEP_LIMIT})
        ).all()
        # #28: failed 잔존 존재 여부 — 있으면 _apply에서 bounded 회수한다(부분 인덱스 조회).
        has_failed_deletes = (
            await session.scalar(text(
                "SELECT 1 FROM mem0_memory_registry WHERE provider_delete_state='failed' LIMIT 1"
            ))
        ) is not None

    # 독립적인 사용자별 예약 트랜잭션이다. 아래 fenced finalize는 sweep job 행을 먼저
    # 잠그므로, 그 안에서 요약 예약의 사용자 잠금을 잡지 않는다.
    retention_resume = await _resume_retention_checkpoints()
    retention_detail = {
        "checkpoint_candidates": retention_resume.candidates,
        "checkpoint_enqueued": retention_resume.enqueued,
        "checkpoint_replayed": retention_resume.replayed,
        "checkpoint_busy": retention_resume.busy,
        "checkpoint_blocked": retention_resume.blocked,
    }
    if (not rows and not missing and not shared_missing and not undocumented
            and not stalled and not unjudged and not has_failed_deletes):
        return JobResult(
            result_code=("ok" if retention_resume.enqueued or retention_resume.replayed else
                         "checkpoint_resume_blocked" if retention_resume.blocked else "nothing_stalled"),
            result_detail=retention_detail,
        )

    if stalled:
        _log.warning(
            "기억 사슬이 멈춘 사용자 %s명 — 커서 기준으로 다시 건다 %s",
            len(stalled), [str(r[0])[:8] for r in stalled[:10]],
        )
    if unjudged:
        _log.warning(
            "판정 안 된 기억이 남은 사용자 %s명 — 판정 잡을 다시 건다 %s",
            len(unjudged), [str(r[0])[:8] for r in unjudged[:10]],
        )
    if rows:
        _log.warning("기억 파이프라인 정지 감지 — 죽은 잡 %s건 replay 시도", len(rows))
    if missing:
        _log.warning("일기 임베딩 누락 %s건 — 다시 만든다", len(missing))
    if undocumented:
        _log.warning("회상 색인에 없는 일기 %s건 — 문서를 만든다", len(undocumented))
    revived: list[str] = []
    repaired = 0
    indexed = 0
    shared_repaired = 0
    resumed = 0
    rejudged = 0

    recovered_deletes = 0

    async def _apply(session) -> None:
        nonlocal repaired, shared_repaired, indexed, resumed, rejudged, recovered_deletes
        document_users = await _try_lock_document_users(session, undocumented)
        # 시간 칸을 키에 넣어 한 시간에 한 번만 다시 건다. 매 틱마다 새 잡을 쌓지 않는다.
        bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")

        # 죽은 잡 되살리기(`rows`)가 이미 집어간 사람은 건너뛴다. 둘 다 걸면 같은 구간을
        # 두 번 추출해 LLM 호출이 두 배로 든다(데이터는 결정적 id라 수렴한다).
        # 배치(#15a): 행당 enqueue 왕복 N회 → enqueue_many 1문. 카운트 의미는 동일하다
        # (rowcount = 새로 삽입된 수 = 기존 `made is not None` 합).
        replaying = {u for _j, u in rows}
        current_stalled = await _current_stalled(session,
            [row for row in stalled if row[0] not in replaying])
        resumed += await jobs.enqueue_many(
            session,
            queue=jobs.QUEUE_MEMORY,
            job_type=memory_pipeline.JOB_MEM0_INGEST,
            items=[
                (
                    user_id,
                    f"resume:{user_id}:c{cursor}:g{generation}:{bucket}",
                    {"turn_seq": int(next_turn), "privacy_epoch": int(epoch)},
                )
                for user_id, cursor, generation, epoch, next_turn in current_stalled
                if next_turn is not None and user_id not in replaying
            ],
        )

        rejudged += await jobs.enqueue_many(
            session,
            queue=jobs.QUEUE_MEMORY,
            job_type=memory_pipeline.JOB_MEM0_CONSOLIDATE,
            items=[
                (
                    user_id,
                    f"rejudge:{user_id}:{turn_seq}:g{generation}:{bucket}",
                    {"turn_seq": int(turn_seq), "privacy_epoch": int(epoch)},
                )
                for user_id, turn_seq, generation, epoch in unjudged
            ],
        )

        # operation_id가 replay의 멱등 키다. 같은 잡을 두 번 훑어도 새 잡이 겹치지 않게
        # 원본 job_id에서 결정적으로 만든다.
        replayed_ids = await jobs.replay_dead_many(
            session,
            pairs=[
                (job_id, uuid.uuid5(_REPLAY_NAMESPACE, str(job_id)))
                for job_id, _user_id in rows
            ],
        )
        revived.extend(str(user_id) for job_id, user_id in rows if job_id in replayed_ids)

        if missing:
            bumped = (
                await session.execute(
                    _BUMP_REPAIR_BATCH,
                    {
                        "user_ids": [u for u, _d, _a in missing],
                        "diary_ids": [d for _u, d, _a in missing],
                        "repair_attempts": [int(a) for _u, _d, a in missing],
                        "max_attempts": MAX_EMBEDDING_REPAIR,
                    },
                )
            ).all()  # RETURNING에 없는 행 = 그 사이 채워졌거나 상한에 닿았다
            # 원래 잡과 같은 중복 방지 키를 쓰면 죽은 잡에 막혀 다시 걸리지 않는다.
            # 회차를 키에 넣어 매번 새 잡이 되게 한다.
            repaired += await _enqueue_private_repairs(session, bumped)

        shared_repaired += await _enqueue_shared_repairs(session, shared_missing)

        for user_id, diary_id in undocumented:
            if user_id not in document_users:
                continue
            # 문서 생성은 upsert가 한다. 같은 조건을 두 곳에 두지 않으려고 그 함수를 그대로
            # 부른다(정상 상태 0건이라 배치화 실익도 없다). 성공하면 임베딩 잡도 함께 걸린다.
            await diary_recall_repo.upsert_diary_recall_document(
                session, user_id=user_id, diary_id=diary_id
            )
            indexed += 1

        # #28: provider 삭제 failed 잔존물 회수 — pending으로 되돌리고, 해당 사용자에게
        # provider_delete 잡을 다시 건다(시간 칸 dedup — 한 시간에 한 번만).
        failed_users = [
            r[0] for r in (
                await session.execute(_RECOVER_FAILED_DELETES, {"limit": SWEEP_LIMIT})
            ).all()
        ]
        recovered_deletes = len(failed_users)
        if failed_users:
            distinct = sorted({u for u in failed_users}, key=str)
            epochs = dict(
                (await session.execute(_PIPELINE_EPOCHS, {"user_ids": distinct})).all()
            )
            await jobs.enqueue_many(
                session,
                queue=jobs.QUEUE_MAINTENANCE,
                job_type=memory_pipeline.JOB_MEM0_PROVIDER_DELETE,
                items=[
                    (
                        u,
                        f"provdel:recover:{u}:{bucket}",
                        {"privacy_epoch": int(epochs.get(u, 0)), "limit": 50},
                    )
                    for u in distinct
                ],
            )

    return JobResult(
        result_code="ok",
        result_detail={
            **retention_detail,
            "stalled_users": len(stalled),
            "resumed": resumed,
            "unjudged_users": len(unjudged),
            "rejudged": rejudged,
            "dead_found": len(rows),
            "revived": len(revived),
            "missing_embedding": len(missing),
            "repair_enqueued": repaired,
            "missing_shared_embedding": len(shared_missing),
            "shared_repair_enqueued": shared_repaired,
            "missing_document": len(undocumented),
            "document_created": indexed,
            "has_failed_deletes": has_failed_deletes,
        },
        apply_domain=_apply,
    )


consumer.register(JOB_MEMORY_SWEEP, handle_memory_sweep)
