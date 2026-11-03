"""대화 요약 checkpoint 잡 핸들러(W11) — `conversation_checkpoint`. content 큐.

W7 계약 위에서 지키는 것(어기면 조용히 깨진다):

1. **도메인 쓰기는 fenced finalize와 같은 트랜잭션**(`JobResult.apply_domain`). lease를 잃었으면
   `jobs.finalize_success`가 apply_domain을 아예 부르지 않으므로 늦게 돌아온 소비자가 옛 요약을
   덮어쓰지 않는다(`worker/memory_jobs.py`와 같은 방식).
2. **LLM 호출 중에는 DB 세션을 쥐지 않는다.** 읽기용 짧은 세션을 먼저 닫고 호출한다.
3. **입력이 그대로인지 확인한 뒤에만 요약한다.** 범위 메시지를 다시 읽어 `source_hash`를 재계산하고
   payload와 다르면 버린다(succeeded + 사유 코드) — 그 사이 앞선 checkpoint가 생겼거나 근거가
   달라진 것이고, 재시도해도 같은 결과다.
4. 🔴 **Summary는 Fact가 아니다.** 이 파일은 기억 추출·reconcile 잡을 **만들지 않는다**(§W11-7).
   요약을 사실로 되먹이면 근거가 요약으로 오염되고 W8의 evidence 계약이 깨진다.
5. **킬스위치가 off면 아무 일도 하지 않는다** — 이미 큐에 있던 잡도 쓰기 없이 succeeded로 끝낸다.
"""
from __future__ import annotations

import logging
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.db import get_sessionmaker
from app.services import checkpoint, checkpoint_repo, usage_ledger, context_retention, config_store, jobs
from app.models.message import SUMMARY_EXCLUDED_MESSAGE_KINDS
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobCancelled, JobDeferred, JobFatal, JobResult, JobRetry

_log = logging.getLogger("moly-worker")

# finalize 사유 코드(계약) — 대시보드/쿼리가 이 값을 본다.
RESULT_OK = "ok"
RESULT_DISABLED = "checkpoint_disabled"
RESULT_ALREADY_CHECKPOINTED = "already_checkpointed"
RESULT_SOURCE_CHANGED = "source_changed"
# 잡을 만든 뒤 "잊어줘"가 실행됐다 — 그 대화를 다시 요약해 넣으면 안 된다(재시도해도 영원히 stale).
RESULT_STALE_GENERATION = "stale_generation"
# 요약 대상 구간에 잊어줘가 닫은 메시지가 섞여 있다 — 요약하면 지운 내용이 되살아난다.
RESULT_SOURCE_CLOSED = "source_range_closed"


@dataclass(frozen=True, slots=True)
class _Request:
    """잡 payload에서 꺼낸 요약 요청. 여기까지 왔으면 형식은 이미 검증됐다."""

    user_id: uuid.UUID
    through_message_id: int
    source_message_ids: tuple[int, ...]
    source_hash: str
    version: str
    previous_id: str | None
    previous_through_message_id: int | None
    memory_generation: int


def _int_field(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise JobFatal("invalid_payload")
    return value


def _generation_field(payload: dict) -> int:
    """payload의 memory_generation. **0이 정상값**이라 _int_field(양수 강제)를 쓸 수 없다."""
    value = payload.get("memory_generation")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JobFatal("invalid_payload")
    return value


def _parse(job: ClaimedJob) -> _Request:
    """payload 검증 — 어기면 재시도해도 같으므로 즉시 dead."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    if payload.get("reason") not in {None, "input_pressure"}:
        raise JobFatal("invalid_payload")
    if payload.get("schema_version") != checkpoint.SCHEMA_VERSION:
        raise JobFatal("unsupported_payload_schema")
    if job.user_id is None:
        raise JobFatal("invalid_payload")
    raw_ids = payload.get("source_message_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise JobFatal("invalid_payload")
    ids: set[int] = set()
    for raw in raw_ids:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0 or raw in ids:
            raise JobFatal("invalid_payload")
        ids.add(raw)
    source_hash = payload.get("source_hash")
    version = payload.get("summarizer_version")
    if not isinstance(source_hash, str) or not source_hash:
        raise JobFatal("invalid_payload")
    if not isinstance(version, str) or not version:
        raise JobFatal("invalid_payload")
    prev_id = payload.get("previous_checkpoint_id")
    if prev_id is not None and not isinstance(prev_id, str):
        raise JobFatal("invalid_payload")
    prev_through = payload.get("previous_through_message_id")
    if prev_through is not None and (
        isinstance(prev_through, bool) or not isinstance(prev_through, int)
    ):
        raise JobFatal("invalid_payload")
    return _Request(
        user_id=job.user_id,
        through_message_id=_int_field(payload, "through_message_id"),
        source_message_ids=tuple(sorted(ids)),
        source_hash=source_hash,
        version=version,
        previous_id=prev_id,
        previous_through_message_id=prev_through,
        memory_generation=_generation_field(payload),
    )


def _chain_matches(previous: checkpoint.Checkpoint | None, req: _Request) -> bool:
    """지금 DB의 최신 checkpoint가 이 잡이 전제한 이전 마디와 같은가.

    다르면(그 사이 다른 checkpoint가 들어왔다) 이 잡의 `source_hash`는 더 이상 성립하지 않는다.
    """
    return (str(previous.id) if previous is not None else None) == req.previous_id


async def _generation_matches(session: AsyncSession, req: _Request) -> bool:
    """잡을 만든 시점과 지금의 기억 세대가 같은가. 다르면 그 사이에 forget이 있었다.

    **이른 종료용이다** — LLM을 괜히 부르지 않으려는 것이고, 최종 방어는 `checkpoint_repo.insert`가
    같은 문장 안에서 하는 세대 검사다(읽고 나서 쓰는 사이의 경합을 잠금 없이 없앤다).
    """
    current = await checkpoint_repo.read_memory_generation(session, req.user_id)
    if current == req.memory_generation:
        return True
    _log.info(
        "요약 잡 stale — user=%s payload_gen=%d current_gen=%d(그 사이 forget)",
        req.user_id, req.memory_generation, current,
    )
    return False


async def _still_applicable(
    session: AsyncSession, req: _Request
) -> tuple[bool, checkpoint.Checkpoint | None]:
    """fenced finalize 트랜잭션 안에서 하는 마지막 확인(최신 checkpoint를 fresh read).

    어긋나면 조용히 건너뛴다 — 여기서 예외를 던지면 finalize가 통째로 롤백돼 lease 만료까지 잡이
    running으로 남는다(memory_jobs와 같은 규칙).
    """
    # 세대부터 본다. LLM 구간(DB 커넥션 0)에서 forget이 끼면 checkpoint가 전량 지워져
    # previous=None이 되는데, 그 상태가 "첫 요약"과 구분이 안 돼 체인 검사만으로는 통과한다.
    if not await _generation_matches(session, req):
        return False, None
    previous = (await checkpoint_repo.load_latest(session, req.user_id, retention_enabled=True)
                if req.version == checkpoint.RETENTION_SUMMARIZER_VERSION
                else await checkpoint_repo.load_latest(session, req.user_id))
    if previous is not None and previous.through_message_id >= req.through_message_id:
        return False, previous
    if not _chain_matches(previous, req):
        return False, previous
    return True, previous


async def _load_sources(
    session: AsyncSession, req: _Request, previous: checkpoint.Checkpoint | None
) -> list[checkpoint.SourceMessage]:
    """요약 대상 구간을 DB에서 다시 읽는다 — payload의 목록을 그대로 믿지 않는다.

    **하한은 이전 checkpoint가 아니라 payload가 지목한 첫 메시지**다. producer가 넘기는 세그먼트는
    `messages` 전체가 아니라 **앵커 이후**라서, 앵커 리셋을 겪은 유저에겐 앵커 이전 이력이 DB에 그대로
    남아 있다. 하한을 `(이전 checkpoint or 0)`으로 잡으면 그 옛 메시지까지 읽혀 목록이 어긋나고,
    checkpoint가 0건인 유저는 **영원히** `source_changed`로만 끝난다(체인이 시작되지 않는다).

    하한을 payload에서 받아도 신뢰 문제는 없다 — 읽어온 목록으로 `source_hash`를 다시 계산해
    payload와 대조하고, 그 해시에는 이전 checkpoint의 `(id, source_hash)`가 물려 있다. 즉 하한을
    조작해도 유효한 요약으로 성립시킬 수 없다.

    payload id 목록과 다르면(메시지가 지워졌거나 사이에 새 행이 끼었다) 빈 목록을 돌려 호출측이
    `source_changed`로 끝내게 한다.
    """
    from_id = req.source_message_ids[0]
    if previous is not None and from_id <= previous.through_message_id:
        # 이전 checkpoint가 이미 덮은 구간과 겹친다 = 같은 대화를 두 번 요약하게 된다.
        return []
    range_options = ({"include_excluded": True}
                     if req.version == checkpoint.RETENTION_SUMMARIZER_VERSION else {})
    messages = await checkpoint_repo.load_range(
        session,
        req.user_id,
        after_id=from_id - 1,  # 열린 하한 → `[from_id, through]` 닫힌 구간
        through_id=req.through_message_id,
        max_rows=len(req.source_message_ids) + 1,  # +1 = 초과분(사이에 낀 새 행) 탐지용
        **range_options,
    )
    if tuple(sorted(m.id for m in messages)) != req.source_message_ids:
        return []
    return messages


async def _wake_ingest_dependency(req: _Request) -> None:
    """No checkpoint finalize/job lock or original read transaction is held."""
    try:
        async with get_sessionmaker()() as session:
            await checkpoint_repo.wake_checkpoint_ingest(
                session, user_id=req.user_id, generation=req.memory_generation,
                through_id=req.through_message_id,
            )
            await session.commit()
    except Exception as exc:
        # Keep the durable target and dependency deferral. A transient wake
        # failure must not spend the checkpoint's actual model retry budget.
        _log.warning("checkpoint_dependency_wake_failed error=%s", type(exc).__name__)


def _completed_binding(req: _Request, payload: dict) -> str:
    fixed = {"user_id": str(req.user_id), "generation": req.memory_generation,
             "through": req.through_message_id, "ids": list(req.source_message_ids),
             "source_hash": req.source_hash, "parent": req.previous_id,
             "parent_through": req.previous_through_message_id,
             "version": req.version, "schema_version": payload.get("schema_version"),
             "reason": payload.get("reason"),
             "privacy_epoch": payload.get("privacy_epoch"),
             "proof": payload.get("completed_proof")}
    return hashlib.sha256(json.dumps(fixed, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _validate_completed(req, payload, sources, previous, language):
    if payload.get("completed_binding") != _completed_binding(req, payload):
        raise checkpoint.CheckpointError("progress_completed_binding")
    allowed = [message for message in sources if message.kind not in SUMMARY_EXCLUDED_MESSAGE_KINDS]
    return checkpoint.validate_completed_progress(messages=allowed, previous=previous,
        language=language, state=payload.get("completed_state"), proof=payload.get("completed_proof"))


class _PrivacyLockBusy(JobDeferred):
    """Roll finalize back: the same durable job survives a temporary barrier lock."""

    def __init__(self, reason="checkpoint_privacy_busy"):
        super().__init__(reason, delay_s=5)


_STORE_COMPLETED_PARENT = text("""
UPDATE async_jobs SET payload=CAST(:payload AS jsonb),payload_hash=:payload_hash
WHERE id=:job_id AND user_id=:user_id AND job_type='conversation_checkpoint'
  AND state='succeeded' AND payload_redacted_at IS NULL
  AND (payload_expires_at IS NULL OR payload_expires_at>now())
  AND payload->>'source_hash'=:source_hash
  AND payload->'privacy_epoch'=to_jsonb(CAST(:privacy_epoch AS bigint))
RETURNING id
""")


async def _store_completed_parent(session, job, payload) -> bool:
    """Called only inside fenced apply; preserve existing expiry/redaction policy.

    Even if disabled after its model call, an already computed, revalidated final
    draft may replace this same parent's payload. It creates no job or publication.
    """
    wire = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    row = (await session.execute(_STORE_COMPLETED_PARENT, {
        "job_id": job.id, "user_id": job.user_id, "payload": wire,
        "payload_hash": hashlib.sha256(wire.encode("utf-8")).hexdigest(),
        "source_hash": job.payload["source_hash"], "privacy_epoch": job.payload["privacy_epoch"],
    })).first()
    return row is not None


async def _writable_privacy_epoch(session, req, payload) -> bool:
    if await context_retention.privacy_epoch_matches(session, user_id=req.user_id,
            privacy_epoch=payload.get("privacy_epoch"), lock=True):
        return True
    if await context_retention.privacy_epoch_matches(session, user_id=req.user_id,
            privacy_epoch=payload.get("privacy_epoch")):
        # Never enqueue a new artifact without the barrier lock. finalize rolls
        # back and lease recovery retries the original job with its same draft.
        raise _PrivacyLockBusy("checkpoint_privacy_busy")
    return False  # actual deletion/epoch change: discard, never rebind.


async def _handle_retention_publish(job: ClaimedJob) -> JobResult:
    """A durable completed draft waits here without repeating a single model call."""
    req, payload = _parse(job), job.payload
    if req.version != checkpoint.RETENTION_SUMMARIZER_VERSION:
        raise JobFatal("invalid_payload")
    async with get_sessionmaker()() as session:
        if (not await context_retention.enabled_now(session)
                or not await context_retention.adopted_user(session, user_id=req.user_id)):
            return JobResult(result_code=RESULT_DISABLED)
        if not await context_retention.privacy_epoch_matches(session, user_id=req.user_id,
                privacy_epoch=payload.get("privacy_epoch")):
            raise JobCancelled("checkpoint_privacy_changed")
        applicable, previous = await _still_applicable(session, req)
        if not applicable:
            return JobResult(result_code=RESULT_SOURCE_CHANGED)
        user = await checkpoint_repo.load_user_state(session, req.user_id)
        if user is None:
            raise JobCancelled("user_deleted")
        _, language = user
        sources = await _load_sources(session, req, previous)
        if not sources or checkpoint.source_hash(previous=previous, messages=sources,
                version=req.version) != req.source_hash:
            return JobResult(result_code=RESULT_SOURCE_CHANGED)
        try:
            summary = _validate_completed(req, payload, sources, previous, language)
            await context_retention.validate_progress_sources(session, user_id=req.user_id,
                state=payload["completed_state"])
        except (checkpoint.CheckpointError, context_retention.RetentionError) as exc:
            raise JobFatal("checkpoint_completed_proof_invalid") from exc
        if not await context_retention.processing_complete(session, user_id=req.user_id,
                generation=req.memory_generation, through_id=req.through_message_id):
            await context_retention.enqueue_contract_processing(session, user_id=req.user_id,
                generation=req.memory_generation, through_id=req.through_message_id)
            await session.commit()
            await _wake_ingest_dependency(req)
            raise JobDeferred("checkpoint_source_processing_pending", delay_s=5)
        index = await checkpoint_repo.count(session, req.user_id, retention_enabled=True) + 1

    async def _apply(session):
        if (not await context_retention.enabled_now(session, lock=True)
                or not await context_retention.adopted_user(session, user_id=req.user_id, lock=True)):
            return
        if not await _writable_privacy_epoch(session, req, payload):
            return
        applicable, fresh_parent = await _still_applicable(session, req)
        if not applicable:
            return
        current = await _load_sources(session, req, fresh_parent)
        if not current or checkpoint.source_hash(previous=fresh_parent, messages=current,
                version=req.version) != req.source_hash:
            return
        _validate_completed(req, payload, current, fresh_parent, language)
        await context_retention.validate_progress_sources(session, user_id=req.user_id,
            state=payload["completed_state"])
        if not await context_retention.processing_complete(session, user_id=req.user_id,
                generation=req.memory_generation, through_id=req.through_message_id):
            # The handler's earlier true result can regress. Persist the SAME
            # draft under the finalize lease fence instead of silently losing it.
            await jobs.enqueue(session, queue=jobs.QUEUE_CONTENT,
                job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT, user_id=req.user_id,
                dedup_key=f"checkpoint-publish-wait:{job.id}", payload=payload,
                available_at=datetime.now(timezone.utc) + timedelta(seconds=5),
                max_attempts=max(1, job.max_attempts - job.attempt + 1))
            return
        inserted = await checkpoint_repo.insert(session, user_id=req.user_id,
            through_message_id=req.through_message_id, summary=summary, source_hash=req.source_hash,
            expected_generation=req.memory_generation, version=req.version,
            state_json=payload["completed_state"], validation_json={
                "source_message_ids": list(req.source_message_ids), "processing_complete": True,
                "policy_excluded_ids": [m.id for m in current if m.kind in SUMMARY_EXCLUDED_MESSAGE_KINDS],
                "completed_proof": payload["completed_proof"],
                "completed_binding": payload["completed_binding"],
                "privacy_epoch": payload["privacy_epoch"],
            }, previous_id=fresh_parent.id if fresh_parent else None,
            coverage_from=req.source_message_ids[0])
        if inserted is None:
            return
        await checkpoint_repo.clear_completed_pressure(session, user_id=req.user_id,
            generation=req.memory_generation, through_id=req.through_message_id)
        if index % max(int(settings.context_checkpoint_reverify_every), 1) == 0:
            await jobs.enqueue(session, queue=jobs.QUEUE_CONTENT,
                job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT, user_id=req.user_id,
                dedup_key=f"checkpoint-reverify:{req.user_id}:{req.memory_generation}:{inserted}:0",
                payload={"mode": "reverify", "schema_version": checkpoint.SCHEMA_VERSION,
                    "summarizer_version": checkpoint.RETENTION_SUMMARIZER_VERSION,
                    "memory_generation": req.memory_generation, "checkpoint_id": str(inserted),
                    "through_message_id": req.through_message_id, "after_message_id": 0,
                    "privacy_epoch": payload["privacy_epoch"], "manifests": []}, max_attempts=2)
        else:
            await checkpoint_repo.reserve_remaining_pressure(session, user_id=req.user_id,
                generation=req.memory_generation, published_through_id=req.through_message_id,
                privacy_epoch=payload["privacy_epoch"])

    return JobResult(result_code=RESULT_OK, apply_domain=_apply)


async def handle_checkpoint(job: ClaimedJob) -> JobResult:
    if (job.payload or {}).get("mode") == "reverify":
        return await _handle_retention_reverify(job)
    if (job.payload or {}).get("mode") == "publish_retention":
        return await _handle_retention_publish(job)
    payload_version = (job.payload or {}).get("summarizer_version")
    if not settings.context_checkpoint_enabled and payload_version != checkpoint.RETENTION_SUMMARIZER_VERSION:
        return JobResult(result_code=RESULT_DISABLED)
    req = _parse(job)

    async with get_sessionmaker()() as session:
        retention = req.version == checkpoint.RETENTION_SUMMARIZER_VERSION
        if retention:
            raw = await config_store.get_config_values(session, ["context_retention_v2_enabled"])
            if raw.get("context_retention_v2_enabled", settings.context_retention_v2_enabled) is not True:
                return JobResult(result_code=RESULT_DISABLED)
            if not await context_retention.adopted_user(session, user_id=req.user_id):
                return JobResult(result_code=RESULT_DISABLED)
            if not await context_retention.privacy_epoch_matches(session, user_id=req.user_id,
                    privacy_epoch=(job.payload or {}).get("privacy_epoch")):
                raise JobCancelled("checkpoint_privacy_changed")
        state = await checkpoint_repo.load_user_state(session, req.user_id)
        if state is None:  # 탈퇴 — 처리 의미가 사라졌다(경보 대상 아님)
            raise JobCancelled("user_deleted")
        nickname, language = state

        # 세대부터 본다. 잡을 만든 뒤 forget이 실행됐으면 그 대화는 다시 요약하면 안 된다.
        # forget이 checkpoint를 전량 지운 직후라 previous=None이 되어, 체인 검사만으로는
        # "첫 요약"으로 보이고 그냥 통과한다 — 잊어달라고 한 대화가 요약으로 되살아난다.
        if not await _generation_matches(session, req):
            return JobResult(result_code=RESULT_STALE_GENERATION)

        applicable, previous = await _still_applicable(session, req)
        if not applicable:
            return JobResult(
                result_code=(
                    RESULT_ALREADY_CHECKPOINTED
                    if previous is not None
                    and previous.through_message_id >= req.through_message_id
                    else RESULT_SOURCE_CHANGED
                )
            )

        messages = await _load_sources(session, req, previous)
        if not messages:
            return JobResult(result_code=RESULT_SOURCE_CHANGED)
        if checkpoint.source_hash(
            previous=previous, messages=messages, version=req.version,
        ) != req.source_hash:
            # 같은 id 목록이라도 본문이 바뀌었다 = 이 요약의 전제가 깨졌다.
            return JobResult(result_code=RESULT_SOURCE_CHANGED)

        if retention and not await context_retention.processing_complete(
            session, user_id=req.user_id, generation=req.memory_generation,
            through_id=req.through_message_id,
        ):
            await context_retention.enqueue_contract_processing(session, user_id=req.user_id,
                generation=req.memory_generation, through_id=req.through_message_id)
            await session.commit()
            await _wake_ingest_dependency(req)
            # Facts/contracts and unpublished summary pages are independent. Only
            # publication waits for all three to have validated this exact range.

        # 잊어줘가 닫은 메시지 검사는 없다 — 대화형 망각이 제거되면서 `memory_recall_suppressions`
        # 자체가 사라졌다(2026-08-06). 닫히는 구간이 없으므로 이 검사는 항상 통과였다.

        # 몇 번째 checkpoint인가 — 매 N번째는 이전 요약 대신 원본으로 다시 요약해 누적 왜곡을 잰다.
        #
        # 재검증이 읽는 범위는 **그 유저의 경계까지 전체 이력**(`(0, through]`)이다. 체인이 실제로
        # 덮은 구간은 첫 checkpoint의 하한부터인데 그 하한을 따로 보관하지 않으므로, 모자란 쪽이
        # 아니라 **넘치는 쪽**(superset)을 택했다 — 앵커 이전 이력이 더 들어올 뿐 체인이 갖고 있던
        # 내용을 잃지 않는다. 반대로 "이번 구간 원본만"으로 좁히면 매 N번째마다 그 앞 이야기가
        # 통째로 사라진다(캐피가 10번에 한 번씩 기억을 잃는다).
        index = ((await checkpoint_repo.count(session, req.user_id, retention_enabled=True))
                 if retention else (await checkpoint_repo.count(session, req.user_id))) + 1
        every = max(int(settings.context_checkpoint_reverify_every), 1)
        # 재검증은 **검증할 체인이 있을 때만** 의미가 있다. previous가 없는 첫 요약에서 켜면
        # 원본이 비어 빈 입력으로 요약을 시도하고 잡이 재시도 끝에 dead가 된다
        # (기본값 10에선 index%10==0과 previous=None이 양립 불가라 안 드러나지만,
        #  reverify_every=1로 두는 순간 첫 요약부터 막힌다).
        reverify = index % every == 0 and previous is not None and not retention
        originals: list[checkpoint.SourceMessage] = []
        if reverify:
            cap = int(settings.context_checkpoint_reverify_max_messages)
            originals = await checkpoint_repo.load_range(
                session, req.user_id, after_id=None, through_id=req.through_message_id,
                max_rows=cap + 1,
            )
            if len(originals) > cap:
                # 상한을 넘으면 **앞부분이** 잘린다(ORDER BY id ASC LIMIT) — 부분 이력을 전체인 양
                # 요약하지 않고 체인으로 간다.
                _log.info(
                    "요약 재검증 생략(원본 %d건 > 상한 %d) — 체인 요약으로 진행", len(originals), cap
                )
                reverify, originals = False, []

    # ── 여기서부터 DB 커넥션 0 (LLM 구간) ──
    inputs = originals if reverify else messages
    if retention:
        inputs = [message for message in inputs if message.kind not in SUMMARY_EXCLUDED_MESSAGE_KINDS]
    previous_summary = None if reverify or previous is None else previous.summary
    state_json = None
    call = None
    continuation = None
    try:
        if retention and not inputs:
            state_json = context_retention.validate_state(previous.state_json if previous else None)
            summary = context_retention.render_state(state_json)
        elif retention:
            summary, state_json, call = await checkpoint.summarize_progress(
                messages=inputs, previous=previous, language=language, nickname=nickname,
                continuation_state=(job.payload or {}).get("progress_state"),
                start_chunk=(job.payload or {}).get("progress_next_chunk", 0),
                continuation_manifest=(job.payload or {}).get("progress_manifest"),
                max_batches=1,
                ledger=usage_ledger.LedgerContext(lane=usage_ledger.LANE_BACKGROUND,
                    purpose="context_summary", user_id=job.user_id, job_id=job.id, attempt=job.attempt),
            )
        else:
            summary, call = await checkpoint.summarize(
            messages=inputs,
            previous_summary=previous_summary,
            language=language,
            nickname=nickname,
            version=req.version,
            ledger=usage_ledger.LedgerContext(
                lane=usage_ledger.LANE_BACKGROUND, purpose="context_summary",
                user_id=job.user_id, job_id=job.id, attempt=job.attempt,
            ),
            )
    except checkpoint.ProgressContinuation as pending:
        continuation = pending
        state_json, summary = pending.state, ""
    except checkpoint.NameLeakError as e:
        # 마스킹 후에도 실명이 남았다 — 저장하지 않고 재시도(다음 샘플은 통과할 수 있다).
        _log.warning("요약 실명 회귀 검사 실패(재시도) — job_id=%s: %s", job.id, e)
        raise JobRetry("summary_name_leak") from e
    except checkpoint.CheckpointError as e:  # 빈 요약 등 — 다음 샘플에 기대고 재시도
        raise JobRetry("summary_invalid") from e
    except Exception as e:  # noqa: BLE001  # provider 타임아웃·429·일시 장애 — backoff 재시도
        raise JobRetry("summary_llm_failed") from e

    completed_proof = (checkpoint.completed_progress_proof(
        messages=inputs, previous=previous, language=language, state=state_json,
    ) if retention and continuation is None else None)

    async def _apply(session: AsyncSession) -> None:
        # 🔴 여기서 하는 도메인 쓰기는 checkpoint 1행뿐이다. 요약에서 기억 추출 잡을 만들지 않는다.
        # 여기 검사들은 **이른 종료용**이다. 잊어줘와의 경합에 대한 최종 방어는
        # `checkpoint_repo.insert`가 같은 문장 안에서 하는 세대 검사다 — 읽고 나서 쓰는
        # 사이를 잠금으로 막으려 하면 챗과 락 순서가 반대라 교착이 나고, 그걸 NOWAIT로
        # 피하면 재시도 횟수를 갉아먹어 요약이 영구 유실된다.
        writes_enabled = True
        if retention:
            writes_enabled = (await context_retention.enabled_now(session, lock=True)
                and await context_retention.adopted_user(session, user_id=req.user_id, lock=True))
        if retention and not await _writable_privacy_epoch(session, req, job.payload or {}):
            return
        ok, _ = await _still_applicable(session, req)
        if not ok:
            _log.warning(
                "요약 확정 직전 상태 변화로 저장 생략 — user=%s through=%s", req.user_id,
                req.through_message_id,
            )
            return
        if retention:
            fresh_sources = await _load_sources(session, req, previous)
            if not fresh_sources or checkpoint.source_hash(
                previous=previous, messages=fresh_sources, version=req.version,
            ) != req.source_hash:
                return
            await context_retention.validate_progress_sources(session, user_id=req.user_id,
                state=state_json)
        if continuation is not None:
            if not writes_enabled:
                return
            checkpoint.validate_progress_page(messages=inputs, previous=previous, language=language,
                state=state_json, next_chunk=continuation.next_chunk, manifest=continuation.manifest,
                start_chunk=(job.payload or {}).get("progress_next_chunk", 0))
            await context_retention.validate_progress_sources(session, user_id=req.user_id,
                state=state_json)
            await jobs.enqueue(session, queue=jobs.QUEUE_CONTENT,
                job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT, user_id=req.user_id,
                dedup_key=f"checkpoint-progress:{req.user_id}:{req.memory_generation}:{req.source_hash}:{continuation.next_chunk}",
                payload={**job.payload, "progress_state": state_json,
                         "progress_next_chunk": continuation.next_chunk,
                         "progress_manifest": continuation.manifest})
            return
        if retention:
            payload = {key: value for key, value in job.payload.items()
                       if key not in {"progress_state", "progress_next_chunk", "progress_manifest"}}
            payload.update(mode="publish_retention", completed_state=state_json,
                           completed_proof=completed_proof)
            payload["completed_binding"] = _completed_binding(req, payload)
            _validate_completed(req, payload, fresh_sources, previous, language)
            if not await _store_completed_parent(session, job, payload):
                return
            if not writes_enabled:
                return
            await jobs.enqueue(session, queue=jobs.QUEUE_CONTENT,
                job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT, user_id=req.user_id,
                dedup_key=f"checkpoint-publish:{req.user_id}:{req.memory_generation}:{req.source_hash}",
                payload=payload, max_attempts=3)
            return
        inserted_id = await checkpoint_repo.insert(
            session,
            user_id=req.user_id,
            through_message_id=req.through_message_id,
            summary=summary,
            source_hash=req.source_hash,
            expected_generation=req.memory_generation,
            version=req.version,
            **({"state_json": state_json, "validation_json": {
                "source_message_ids": list(req.source_message_ids), "processing_complete": True,
                "policy_excluded_ids": [m.id for m in messages if m.kind in SUMMARY_EXCLUDED_MESSAGE_KINDS],
            }, "previous_id": previous.id if previous else None,
                "coverage_from": req.source_message_ids[0]} if retention else {}),
        )
        if retention and inserted_id is not None:
            await checkpoint_repo.clear_completed_pressure(session, user_id=req.user_id,
                generation=req.memory_generation, through_id=req.through_message_id)
        if retention and inserted_id is not None and index % every == 0:
            await jobs.enqueue(session, queue=jobs.QUEUE_CONTENT,
                job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT, user_id=req.user_id,
                dedup_key=f"checkpoint-reverify:{req.user_id}:{req.memory_generation}:{inserted_id}:0",
                payload={"mode": "reverify", "schema_version": checkpoint.SCHEMA_VERSION,
                    "summarizer_version": checkpoint.RETENTION_SUMMARIZER_VERSION,
                    "memory_generation": req.memory_generation, "checkpoint_id": str(inserted_id),
                    "through_message_id": req.through_message_id, "after_message_id": 0,
                    "manifests": []}, max_attempts=2)

    return JobResult(
        result_code=("checkpoint_progress_page" if continuation is not None else
                     "checkpoint_draft_ready" if retention else RESULT_OK),
        result_detail={
            "messages": len(inputs),
            "chars": len(summary),
            "reverified": reverify,
            "billable": (continuation.billable if continuation is not None
                         else call.billable if call is not None else 0),
        },
        apply_domain=_apply,
    )


async def _handle_retention_reverify(job: ClaimedJob) -> JobResult:
    """원문을 페이지별로 다시 확인한다. 완료 전 중간 복구 상태는 발행하지 않는다."""
    payload = job.payload or {}
    if job.user_id is None:
        raise JobFatal("missing_user")
    generation = payload.get("memory_generation")
    after = payload.get("after_message_id", 0)
    through = payload.get("through_message_id")
    if any(type(value) is not int for value in (generation, after, through)) or generation < 0 or after < 0 or through <= after:
        raise JobFatal("invalid_reverify_payload")
    async with get_sessionmaker()() as session:
        raw = await config_store.get_config_values(session, ["context_retention_v2_enabled"])
        if raw.get("context_retention_v2_enabled", settings.context_retention_v2_enabled) is not True:
            return JobResult(result_code=RESULT_DISABLED)
        if not await context_retention.adopted_user(session, user_id=job.user_id):
            return JobResult(result_code=RESULT_DISABLED)
        if await checkpoint_repo.read_memory_generation(session, job.user_id) != generation:
            return JobResult(result_code=RESULT_STALE_GENERATION)
        current = await checkpoint_repo.load_latest(session, job.user_id, retention_enabled=True)
        if current is None or str(current.id) != payload.get("checkpoint_id") or current.through_message_id != through:
            return JobResult(result_code=RESULT_SOURCE_CHANGED)
        profile = await checkpoint_repo.load_user_state(session, job.user_id)
        if profile is None:
            raise JobCancelled("user_deleted")
        page = await checkpoint_repo.load_range(session, job.user_id, after_id=after,
            through_id=through, max_rows=120, include_excluded=True)
    if not page:
        return JobResult(result_code=RESULT_SOURCE_CHANGED)
    boundary = page[-1].id
    completed = boundary == through or len(page) < 120
    initial = context_retention.validate_state(current.state_json)
    working = context_retention.validate_state(payload.get("draft_state", current.state_json))
    allowed = [message for message in page if message.kind not in SUMMARY_EXCLUDED_MESSAGE_KINDS]
    page_hash = checkpoint.source_hash(previous=None, messages=page,
                                       version=checkpoint.RETENTION_SUMMARIZER_VERSION)
    continuation = None
    if allowed:
        reviewed = checkpoint.Checkpoint(current.id, current.through_message_id, current.summary,
            current.version, current.source_hash, working, current.validation_json)
        try:
            _, working, _ = await checkpoint.summarize_progress(messages=allowed,
                previous=reviewed, language=profile[1], nickname=profile[0],
                continuation_state=payload.get("progress_state"),
                start_chunk=payload.get("progress_next_chunk", 0),
                continuation_manifest=payload.get("progress_manifest"), max_batches=1,
                ledger=usage_ledger.LedgerContext(lane=usage_ledger.LANE_BACKGROUND,
                    purpose="context_summary", user_id=job.user_id, job_id=job.id, attempt=job.attempt))
        except checkpoint.ProgressContinuation as pending:
            continuation, working = pending, pending.state
        except Exception as exc:
            raise JobRetry("reverify_invalid") from exc
        # 검증된 현재 항목은 오래된 원문을 다시 읽었다는 이유로 변경하지 않는다.
        protected = {item["id"]: item for item in initial["items"]}
        recovered = [item for item in working["items"] if item["id"] not in protected]
        # 과거 페이지를 읽었다고 종료/보관한 주제를 되살리지 않는다. 마지막
        # 게시 이력의 상태와 근거를 확인하고, 현재 작업 목록만 복원한다.
        async with get_sessionmaker()() as session:
            current_recovered = []
            for item in recovered:
                historical = await context_retention.load_historical_item(session,
                    user_id=job.user_id, generation=generation, item_id=item["id"])
                if historical is None or historical["item"]["status"] == "active":
                    current_recovered.append(item)
        recovered = current_recovered
        working["items"] = sorted([*protected.values(), *recovered], key=lambda item: item["id"])
        context_retention.validate_state(working)
    manifests = [*(payload.get("manifests") or []), {"after": after, "through": boundary, "hash": page_hash}]

    async def _apply(session) -> None:
        if not await context_retention.enabled_now(session, lock=True):
            return
        if not await context_retention.adopted_user(session, user_id=job.user_id, lock=True):
            return
        latest = await checkpoint_repo.load_latest(session, job.user_id, retention_enabled=True)
        if latest is None or latest.id != current.id:
            return
        if await checkpoint_repo.read_memory_generation(session, job.user_id) != generation:
            return
        # 직전 페이지와 완료 시 모든 실제 입력 페이지의 현재 지문을 다시 대조한다.
        checks = manifests if completed else manifests[-1:]
        for manifest in checks:
            sources = await checkpoint_repo.load_range(session, job.user_id,
                after_id=manifest["after"], through_id=manifest["through"], max_rows=121,
                include_excluded=True)
            digest = checkpoint.source_hash(previous=None, messages=sources,
                                             version=checkpoint.RETENTION_SUMMARIZER_VERSION)
            if digest != manifest["hash"]:
                return
        if continuation is not None:
            await context_retention.validate_progress_sources(session, user_id=job.user_id,
                state=working)
            await jobs.enqueue(session, queue=jobs.QUEUE_CONTENT,
                job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT, user_id=job.user_id,
                dedup_key=f"checkpoint-reverify-progress:{job.user_id}:{generation}:{current.id}:{after}:{continuation.next_chunk}",
                payload={**payload, "progress_state": working,
                         "progress_next_chunk": continuation.next_chunk,
                         "progress_manifest": continuation.manifest}, max_attempts=2)
            return
        if not completed:
            next_payload = {key: value for key, value in payload.items()
                            if key not in {"progress_state", "progress_next_chunk", "progress_manifest"}}
            await jobs.enqueue(session, queue=jobs.QUEUE_CONTENT,
                job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT, user_id=job.user_id,
                dedup_key=f"checkpoint-reverify:{job.user_id}:{generation}:{current.id}:{boundary}",
                payload={**next_payload, "after_message_id": boundary, "draft_state": working,
                         "manifests": manifests}, max_attempts=2)
            return
        if not await context_retention.processing_complete(session, user_id=job.user_id,
            generation=generation, through_id=through):
            return
        await context_retention.validate_progress_sources(session, user_id=job.user_id, state=working)
        digest = hashlib.sha256(json.dumps({"parent": current.source_hash,
            "manifests": manifests, "state": working}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        inserted = await checkpoint_repo.insert(session, user_id=job.user_id, through_message_id=through,
            summary=context_retention.render_state(working), source_hash=digest,
            expected_generation=generation, version=checkpoint.RETENTION_SUMMARIZER_VERSION,
            state_json=working, validation_json={"processing_complete": True, "reverified": True,
                "page_manifests": manifests}, previous_id=current.id, coverage_from=1)
        if inserted is not None:
            await checkpoint_repo.reserve_remaining_pressure(session, user_id=job.user_id,
                generation=generation, published_through_id=through,
                privacy_epoch=payload.get("privacy_epoch"))
    return JobResult(result_code=("reverify_progress_page" if continuation is not None
                                 else "reverify_complete" if completed else "reverify_page"),
        result_detail={"through": boundary, "pages": len(manifests)}, apply_domain=_apply)


# import 시 1회 등록(모듈 캐시 = 중복 등록 없음). consumer가 이 모듈을 지역 import 하는 이유는
# 순환 import 회피 — 여기서 consumer의 JobResult/JobRetry 계약을 쓰기 때문이다.
consumer.register(checkpoint.JOB_CONVERSATION_CHECKPOINT, handle_checkpoint)
