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
import uuid

from sqlalchemy import text

from app.config import settings
from app.core.db import get_sessionmaker
from app.services import (
    llm,
    mem0_adapter,
    mem0_classifier,
    mem0_consolidation,
    mem0_registry_repo,
    memory_embeddings,
    mem0_extractor,
    mem0_ingest,
    mem0_pipeline,
    memory_pipeline,
    usage_ledger,
)
from app.services.jobs import ClaimedJob
from app.services.mem0_budget import (
    BudgetExceeded,
    memory_consolidation_budget,
    memory_ingest_budget,
)
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

_COMMIT_CANDIDATE = text("""
UPDATE mem0_ingest_candidates SET status='committed', updated_at=now()
WHERE user_id=:user_id AND provider_memory_id=:provider_memory_id AND status='planned'
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
                # registry가 생긴 뒤에야 계획을 닫는다(9.2절). 순서가 뒤집히면 crash 복구가
                # 참조할 planned 행이 사라져 provider에만 남은 벡터를 못 찾는다.
                await session.execute(_COMMIT_CANDIDATE, {
                    "user_id": uid, "provider_memory_id": p.provider_memory_id,
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
        # 판정 잡과 다음 turn 잡을 **같은 fenced transaction**에서 만든다. lease를 잃은
        # 소비자가 후속 잡만 흘리는 일이 없다.
        if outcome.planned:
            await memory_pipeline.enqueue_consolidate(
                session, uid, turn_seq=turn_seq, privacy_epoch=state.privacy_epoch
            )
        else:
            # 기억 0건인 turn은 판정할 게 없다. consolidate 잡을 만들지 않으므로 여기서
            # 커서를 직접 통과시킨다 — 안 그러면 consolidated 커서가 그 turn에 영원히 걸려
            # cutover gate(`consolidated == ingest`)를 절대 통과할 수 없다.
            await memory_pipeline.advance_consolidated_cursor(session, uid, turn_seq=turn_seq)
        await memory_pipeline.enqueue_next_ingest(
            session, uid, cursor=turn_seq, privacy_epoch=state.privacy_epoch
        )

    return JobResult(
        result_code="no_memory" if outcome.no_memory else "ok",
        result_detail={
            "planned": len(outcome.planned),
            "rejected": len(outcome.rejected),
        },
        apply_domain=_advance,
    )


def _adapter():
    """process singleton adapter(9.1절). 프로세스마다 하나만 만든다.

    앱의 asyncpg 엔진이 아니라 **전용 동기 엔진**을 쓴다 — vecs는 동기 SQLAlchemy라
    asyncpg 엔진을 넘기면 MissingGreenlet으로 터진다.
    """
    global _ADAPTER
    if _ADAPTER is None:
        engine = mem0_adapter.build_sync_engine(settings.supabase_db_connection_string)
        client = mem0_adapter.build_client(engine)
        _ADAPTER = mem0_adapter.Mem0VectorIndexAdapter(
            client, collection_name=f"moly_memories_{COLLECTION_VERSION}"
        )
    return _ADAPTER


_ADAPTER = None

consumer.register(JOB_MEM0_INGEST, handle_mem0_ingest)


JOB_MEM0_CONSOLIDATE = "mem0_consolidate"

_CANDIDATE_TEXTS = text("""
SELECT provider_memory_id, candidate_text
FROM mem0_ingest_candidates
WHERE user_id = :user_id AND turn_seq = :turn_seq AND status IN ('planned','committed')
""")


async def handle_mem0_consolidate(job: ClaimedJob) -> JobResult:
    """한 turn의 pending 기억을 판정해 registry 활성 상태를 확정한다(9.4절).

    classifier는 **한 번만** 부른다. invalid graph여도 재질의하지 않고 validator가 보수적으로
    ambiguous로 닫는다 — 재질의는 비용과 비결정성만 늘린다.
    """
    payload = job.payload or {}
    try:
        turn_seq = int(payload["turn_seq"])
    except (KeyError, TypeError, ValueError) as e:
        raise JobFatal("invalid_payload") from e
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id

    async with get_sessionmaker()() as session:
        state = await memory_pipeline.load(session, uid)
        if not state.records_v2:
            raise JobCancelled("not_v2_user")
        pending = await mem0_registry_repo.load_pending(session, uid, turn_seq=turn_seq)
        if not pending:
            # 판정할 게 없다 — 커서만 통과시킨다(정책상 기억 0건인 turn).
            async def _skip(s):
                await memory_pipeline.advance_consolidated_cursor(s, uid, turn_seq=turn_seq)

            return JobResult(result_code="no_pending", apply_domain=_skip)
        pool = await mem0_registry_repo.load_comparison_pool(
            session, uid, turn_seq=turn_seq, limit=mem0_classifier.MAX_EXISTING_CANDIDATES
        )
        texts = dict(
            (r[0], r[1])
            for r in (await session.execute(
                _CANDIDATE_TEXTS, {"user_id": uid, "turn_seq": turn_seq}
            )).all()
        )
        expected_revision = state.revision

    # 기존 기억 본문은 provider payload에서 hydrate한다(registry는 본문 미복제).
    budget = memory_consolidation_budget(total_s=settings.job_content_timeout_s)
    existing_pairs: list[tuple[uuid.UUID, str]] = []
    if pool:
        try:
            fetched = await _adapter().get_many(
                [str(p["provider_memory_id"]) for p in pool],
                user_id=str(uid),
                timeout=budget.timeout_for("search"),
            )
        except BudgetExceeded:
            raise JobRetry("budget_search") from None
        except Exception as e:  # noqa: BLE001
            raise JobRetry("hydrate_failed") from e
        by_provider = {r.id: (r.payload or {}).get("text", "") for r in fetched}
        existing_pairs = [
            (p["id"], by_provider.get(str(p["provider_memory_id"]), ""))
            for p in pool
            if by_provider.get(str(p["provider_memory_id"]))
        ]

    new_pairs = [
        (p["id"], texts.get(p["provider_memory_id"], "")) for p in pending
    ]
    new_pairs = [(i, t) for i, t in new_pairs if t]
    if not new_pairs:
        raise JobRetry("candidate_text_missing")

    known = {i for i, _ in new_pairs} | {i for i, _ in existing_pairs}
    ledger = usage_ledger.LedgerContext(
        lane=usage_ledger.LANE_BACKGROUND, purpose="memory_consolidate",
        user_id=uid, turn_seq=turn_seq, job_id=job.id, attempt=job.attempt,
    )
    try:
        result = await llm.generate(
            mem0_classifier.build_system(),
            [{"role": "user", "content": mem0_classifier.render_pairs(new_pairs, existing_pairs)}],
            model=settings.model_utility,
            max_tokens=mem0_classifier.MAX_OUTPUT_TOKENS,
            timeout=budget.timeout_for("classify"),
            ledger=ledger,
        )
        edges = mem0_classifier.parse(result.text, known_ids=known)
    except BudgetExceeded:
        raise JobRetry("budget_classify") from None
    except mem0_classifier.ClassifierSchemaError as e:
        raise JobRetry("classifier_schema") from e
    except Exception as e:  # noqa: BLE001
        raise JobRetry("classify_failed") from e

    refs = [
        mem0_consolidation.MemoryRef(
            registry_id=p["id"], source_turn_seq=p["source_turn_seq"],
            candidate_hash=p["content_hash"], source_occurred_at=p["occurred_at"],
            is_new=is_new,
        )
        for group, is_new in ((pending, True), (pool, False))
        for p in group
    ]
    verdict = mem0_consolidation.consolidate(refs, edges)
    if verdict.rejected_reasons:
        _log.warning(
            "consolidation graph 거부 %s — job=%s (보수적 ambiguous)",
            verdict.rejected_reasons, job.id,
        )

    async def _publish(session) -> None:
        await mem0_registry_repo.apply_transitions(
            session, uid, verdict.transitions,
            expected_revision=expected_revision,
            classification_version=mem0_classifier.CLASSIFIER_VERSION,
        )
        await memory_pipeline.advance_consolidated_cursor(session, uid, turn_seq=turn_seq)

    return JobResult(
        result_code="ok",
        result_detail={
            "transitions": len(verdict.transitions),
            "ambiguous_components": verdict.ambiguous_components,
        },
        apply_domain=_publish,
    )


consumer.register(JOB_MEM0_CONSOLIDATE, handle_mem0_consolidate)
