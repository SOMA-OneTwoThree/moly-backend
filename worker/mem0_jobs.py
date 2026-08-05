"""mem0 ingest 잡 handler — 파이프라인을 실 provider·DB에 잇는다.

기억 재설계(docs/capi-memory-ARCHITECTURE.md 9.2절, 13.2절).

**이 handler가 지키는 순서**
 1. bootstrap이 `ready`가 아니면 처리하지 않는다 — historical backfill 전에 live turn을
    먼저 색인하면 cursor 연속성이 깨진다
 2. 외부 호출 구간에는 DB session도 advisory lock도 잡지 않는다
 3. 단계별 예산 안에서만 provider를 부른다. 남은 시간이 부족하면 시작하지 않고 retry
 4. 계획 저장 → embedding → upsert → registry pending 순서(파이프라인이 강제)

`mode=legacy` 사용자에겐 애초에 이 잡이 enqueue되지 않는다.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.config import settings
from app.core.db import get_sessionmaker
from app.services import (
    llm,
    mem0_adapter,
    memory_embeddings,
    mem0_extractor,
    mem0_ingest,
    mem0_pipeline,
    memory_pipeline,
    usage_ledger,
)
from app.services.jobs import ClaimedJob
from app.services.mem0_budget import memory_ingest_budget
from worker import consumer
from worker.consumer import JobCancelled, JobFatal, JobResult, JobRetry

_log = logging.getLogger("moly-worker")

JOB_MEM0_INGEST = "mem0_ingest"

# v2 컬렉션 이름·버전. migration 롤이 만든 것만 쓴다(런타임 DDL 금지).
COLLECTION_VERSION = "v2"

_SOURCE_MESSAGES = text("""
SELECT id, sender, content
FROM messages
WHERE user_id = :user_id AND turn_seq = :turn_seq AND kind = 'normal'
ORDER BY id
""")

_STAGE_CANDIDATE = text("""
INSERT INTO mem0_ingest_candidates
  (user_id, turn_seq, candidate_hash, schema_version, extractor_version, normalizer_version,
   provider_memory_id, candidate_text, status)
VALUES (:user_id, :turn_seq, :candidate_hash, :schema_version, :extractor_version,
        :normalizer_version, :provider_memory_id, :candidate_text, 'planned')
ON CONFLICT (user_id, turn_seq, candidate_hash, schema_version, repair_generation) DO NOTHING
""")

_REGISTER_PENDING = text("""
INSERT INTO mem0_memory_registry
  (user_id, provider, collection_version, provider_memory_id, source_turn_seq,
   content_hash, semantic_status, schema_version)
VALUES (:user_id, 'mem0', :collection_version, :provider_memory_id, :turn_seq,
        :content_hash, 'pending', :schema_version)
ON CONFLICT (user_id, provider, collection_version, provider_memory_id) DO NOTHING
""")


async def handle_mem0_ingest(job: ClaimedJob) -> JobResult:
    payload = job.payload or {}
    try:
        turn_seq = int(payload["turn_seq"])
    except (KeyError, TypeError, ValueError) as e:
        raise JobFatal("invalid_payload") from e
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id

    # ── 짧은 read: 상태·원문 확인 후 세션을 닫는다 ──
    async with get_sessionmaker()() as session:
        state = await memory_pipeline.load(session, uid)
        if not state.records_v2:
            raise JobCancelled("not_v2_user")
        if not state.accepts_live_ingest:
            # historical backfill 전 — 이 turn은 아직 차례가 아니다.
            raise JobRetry("bootstrap_not_ready")
        rows = (await session.execute(
            _SOURCE_MESSAGES, {"user_id": uid, "turn_seq": turn_seq}
        )).all()
        profile = (await session.execute(
            text("SELECT nickname, language FROM profiles WHERE id = :u"), {"u": uid}
        )).first()

    if not rows:
        raise JobCancelled("no_source_messages")
    messages = [
        mem0_extractor.SourceMessage(id=r[0], sender=r[1], content=r[2] or "") for r in rows
    ]
    nickname = profile[0] if profile else None
    language = profile[1] if profile else None

    budget = memory_ingest_budget(total_s=settings.job_content_timeout_s)
    ledger = usage_ledger.LedgerContext(
        lane=usage_ledger.LANE_BACKGROUND, purpose="memory_extract",
        user_id=uid, turn_seq=turn_seq, job_id=job.id, attempt=job.attempt,
    )

    # ── 외부 호출: DB 커넥션 0 ──
    async def _extract(timeout: float):
        result = await llm.generate(
            mem0_extractor.build_system(language),
            [{"role": "user", "content": mem0_extractor.render_conversation(messages)}],
            model=mem0_extractor.EXTRACTOR_MODEL,
            max_tokens=mem0_extractor.MAX_OUTPUT_TOKENS,
            timeout=timeout,
            ledger=ledger,
        )
        try:
            candidates, dropped = mem0_extractor.parse(result.text, messages=messages)
        except mem0_extractor.ExtractionSchemaError as e:
            # 계약 위반 — 다음 샘플은 통과할 수 있으니 재시도.
            raise JobRetry("candidate_schema") from e
        if dropped:
            _log.info("근거 미검증 후보 폐기 %d건 — job=%s", len(dropped), job.id)
        return candidates

    async def _embed(texts: list[str], timeout: float) -> list[list[float]]:
        # 통과 후보 전체를 한 번에. 후보마다 부르면 비용·지연이 배로 든다(9.2절).
        return await memory_embeddings.embed_texts(texts)

    async def _upsert(rows_, timeout: float):
        records = [
            mem0_adapter.VectorRecord(id=str(rid), embedding=vec, payload=payload_)
            for rid, vec, payload_ in rows_
        ]
        return await _adapter().insert_many(records, user_id=str(uid), timeout=timeout)

    async def _stage(planned: list[mem0_pipeline.PlannedCandidate]) -> None:
        async with get_sessionmaker()() as session:
            for p in planned:
                await session.execute(_STAGE_CANDIDATE, {
                    "user_id": uid, "turn_seq": turn_seq,
                    "candidate_hash": p.candidate_hash,
                    "schema_version": mem0_ingest.SCHEMA_VERSION,
                    "extractor_version": mem0_extractor.EXTRACTOR_VERSION,
                    "normalizer_version": mem0_ingest.NORMALIZER_VERSION,
                    "provider_memory_id": p.provider_memory_id,
                    "candidate_text": p.text,
                })
            await session.commit()

    async def _register(planned: list[mem0_pipeline.PlannedCandidate]) -> None:
        async with get_sessionmaker()() as session:
            for p in planned:
                await session.execute(_REGISTER_PENDING, {
                    "user_id": uid, "collection_version": COLLECTION_VERSION,
                    "provider_memory_id": p.provider_memory_id, "turn_seq": turn_seq,
                    "content_hash": p.candidate_hash,
                    "schema_version": mem0_ingest.SCHEMA_VERSION,
                })
            await session.commit()

    try:
        outcome = await mem0_pipeline.run_ingest(
            user_id=uid, turn_seq=turn_seq, collection_version=COLLECTION_VERSION,
            budget=budget, extract=_extract, embed=_embed, upsert=_upsert,
            stage_planned=_stage, register_pending=_register,
            nickname=nickname,
        )
    except JobRetry:
        raise
    except ValueError as e:  # 임베딩 개수 불일치 등 — 계약 위반
        raise JobFatal("pipeline_contract") from e
    except Exception as e:  # noqa: BLE001  provider 장애 — backoff 재시도
        raise JobRetry("ingest_failed") from e

    if outcome.skipped_reason:
        raise JobRetry(outcome.skipped_reason.replace(":", "_"))

    async def _advance(session) -> None:
        await memory_pipeline.advance_ingest_cursor(session, uid, turn_seq=turn_seq)

    return JobResult(
        result_code="no_memory" if outcome.no_memory else "ok",
        result_detail={
            "planned": len(outcome.planned),
            "rejected": len(outcome.rejected),
        },
        apply_domain=_advance,
    )


def _adapter():
    """process singleton adapter(9.1절). 프로세스마다 하나만 만든다."""
    from app.core.db import get_engine

    global _ADAPTER
    if _ADAPTER is None:
        client = mem0_adapter.build_client(get_engine().sync_engine)
        _ADAPTER = mem0_adapter.Mem0VectorIndexAdapter(
            client, collection_name=f"moly_memories_{COLLECTION_VERSION}"
        )
    return _ADAPTER


_ADAPTER = None

consumer.register(JOB_MEM0_INGEST, handle_mem0_ingest)
