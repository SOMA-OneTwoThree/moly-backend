"""mem0 ingest 잡 handler — 파이프라인을 실 provider·DB에 잇는다.

기억 재설계(docs/ARCHITECTURE-capi.md 5.2절, 9.1절).

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
    jobs,
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

# 추출 범위는 **턴 하나가 아니라 대화 덩어리**다(커서 다음부터 이번 대상 턴까지).
#
# 한 턴만 보면 한 사건이 여러 턴에 걸칠 때 조각난다. 실제로 유저가 캐피의 모습(귤·안경·목도리)을
# 보고 "귀여워"라고 한 턴에서 `유저가 상대를 귀엽다고 한다`만 남았다 — 무엇을 귀여워했는지가
# 사라졌다. 앞 턴을 안 봤기 때문이다.
#
# 실측(2026-08-08): 유저 발화의 86%가 직전 발화로부터 3분 안에 이어진다. 대화는 턴이 아니라
# 덩어리로 온다. 같은 주제가 여러 턴에 걸치니 비슷한 후보가 반복 생성돼 중복 판정이 7.7%였다.
#
# 근거(evidence)는 `message_id`로 묶이므로(mem0_extractor의 `by_id` 대조) 범위를 넓혀도
# 어느 발화가 근거인지는 그대로 정확하다.
_SOURCE_MESSAGES = text("""
SELECT id, sender, content
FROM messages
WHERE user_id = :user_id AND kind = 'normal'
  AND turn_seq > :from_seq AND turn_seq <= :to_seq
ORDER BY id
""")

_STAGE_CANDIDATE = text("""
INSERT INTO mem0_ingest_candidates
  (user_id, turn_seq, candidate_hash, schema_version, extractor_version, normalizer_version,
   provider_memory_id, candidate_text, status, repair_generation)
VALUES (:user_id, :turn_seq, :candidate_hash, :schema_version, :extractor_version,
        :normalizer_version, :provider_memory_id, :candidate_text, 'planned', :generation)
ON CONFLICT (user_id, turn_seq, candidate_hash, schema_version, repair_generation) DO NOTHING
""")

# 후보의 근거. **이게 없으면 정정을 기존 기억에 연결할 수 없다** — 사용자가 "산책 안 했어"라고
# 해도 어느 기억을 닫아야 하는지 특정할 방법이 없다(감사 지적).
_CANDIDATE_SOURCE = text("""
INSERT INTO mem0_ingest_candidate_sources
  (candidate_id, user_id, source_message_id, source_sender, source_content_hash,
   evidence_start_utf8, evidence_end_utf8, authority, confidence)
SELECT c.id, :user_id, :message_id, :sender, :content_hash,
       :start_utf8, :end_utf8, 'explicit_user', NULL
FROM mem0_ingest_candidates c
WHERE c.user_id = :user_id AND c.provider_memory_id = :provider_memory_id
ON CONFLICT DO NOTHING
""")

# registry 확정 시 같은 근거를 시간 좌표와 함께 옮긴다. timeline 원문 hydration과
# tombstone 검증이 이 표를 읽는다.
_MEMORY_SOURCE = text("""
INSERT INTO mem0_memory_sources
  (registry_id, user_id, source_turn_seq, source_message_id, source_sender,
   evidence_start_utf8, evidence_end_utf8, source_content_hash,
   source_occurred_at, source_activity_date, authority, confidence, extractor_version)
SELECT r.id, :user_id, :turn_seq, m.id, :sender,
       :start_utf8, :end_utf8, :content_hash,
       m.created_at, m.activity_date, 'explicit_user', NULL, :extractor_version
FROM mem0_memory_registry r
JOIN messages m ON m.id = :message_id AND m.user_id = :user_id
WHERE r.user_id = :user_id AND r.provider_memory_id = :provider_memory_id
ON CONFLICT DO NOTHING
""")

_COMMIT_CANDIDATE = text("""
UPDATE mem0_ingest_candidates SET status='committed', updated_at=now()
WHERE user_id=:user_id AND provider_memory_id=:provider_memory_id AND status='planned'
""")

# 재시도가 앞 시도의 계획을 이어받는다. **extractor를 다시 부르면 안 된다** — LLM이라
# 결과가 달라지고, 그러면 앞 시도가 남긴 candidate/registry 행을 아무도 닫지 않는다(실측 사고).
#
# ⚠️ **세대(`repair_generation`)를 반드시 함께 본다.** 이 조건이 없으면 지난 세대가 같은 턴에
#    남긴 후보를 "앞 시도의 계획"으로 착각해 **추출을 통째로 건너뛴다.** 재추출이 정확히 여기서
#    막혔다 — 옛 후보를 그대로 다시 올리고 registry는 `ON CONFLICT DO NOTHING`에 걸려,
#    잡은 성공으로 찍히는데 새 기억은 한 건도 안 생겼다(2026-08-08 실측: 4명 중 2명이 0건).
_RESUME_PLAN = text("""
SELECT provider_memory_id, candidate_hash, candidate_text
FROM mem0_ingest_candidates
WHERE user_id = :user_id AND turn_seq = :turn_seq
  AND schema_version = :schema_version
  AND repair_generation = :generation
  AND status IN ('planned', 'committed')
ORDER BY candidate_hash
""")

# 이 turn에 아직 판정 안 된 기억이 남았는가. 커서를 통과시키기 전에 반드시 본다.
_PENDING_FOR_TURN = text("""
SELECT count(*) FROM mem0_memory_registry
WHERE user_id = :user_id AND source_turn_seq = :turn_seq AND semantic_status = 'pending'
""")

_REGISTER_PENDING = text("""
INSERT INTO mem0_memory_registry
  (user_id, provider, collection_version, provider_memory_id, source_turn_seq,
   content_hash, semantic_status, schema_version)
VALUES (:user_id, 'mem0', :collection_version, :provider_memory_id, :turn_seq,
        :content_hash, 'pending', :schema_version)
ON CONFLICT (user_id, provider, collection_version, provider_memory_id) DO NOTHING
""")


# 덩어리 경계 — 커서 다음부터 **아래 셋 중 먼저 걸리는 곳**까지가 한 덩어리다.
#
#   1. 활동일이 바뀌면 끊는다. 하루 경계 뒷정리(관계 투영·약속 추출·재판정)의 유일한 진입
#      경로가 "앞 턴과 날짜가 다른가"라서, 덩어리가 자정을 넘으면 그 감지가 통째로 막힌다.
#      어제와 오늘은 실제로 다른 자리이기도 하다.
#   2. 앞 턴과의 간격이 idle을 넘으면 끊는다. 실측(2026-08-08)으로 유저 발화의 86%가 3분 안에
#      이어진다 — 그보다 벌어졌으면 다른 자리의 대화로 본다.
#   3. max_turns에서 끊는다. 프롬프트가 모델 상한을 넘지 않게 하는 안전장치다.
#
# 시간은 "의미가 끊기는 지점"이 아니라 **기다릴 수 있는 한계**다. 하루치를 통째로 넣는 게
# 맥락은 가장 온전하지만 그러면 기억이 하루 늦는다(구 방식을 버린 이유). 그래서 조용해지면
# 그 자리는 끝난 것으로 보고 처리한다.
_CHUNK_BOUNDS = text("""
WITH t AS (
  SELECT turn_seq, MIN(created_at) AS ts, MIN(activity_date) AS ad
  FROM messages
  WHERE user_id = :user_id AND kind = 'normal' AND turn_seq IS NOT NULL
    AND turn_seq > :from_seq AND turn_seq <= :upper
  GROUP BY turn_seq
), n AS (
  SELECT turn_seq, ts,
         row_number() OVER (ORDER BY turn_seq) AS rn,
         lag(ts) OVER (ORDER BY turn_seq) AS prev_ts,
         ad <> first_value(ad) OVER (ORDER BY turn_seq ROWS UNBOUNDED PRECEDING) AS day_changed
  FROM t
), brk AS (
  SELECT MIN(rn) AS rn FROM n
  WHERE rn > 1 AND (day_changed OR ts - prev_ts > make_interval(secs => :idle_s))
)
SELECT MIN(turn_seq) AS first_turn,
       MAX(turn_seq) FILTER (
         WHERE rn <= LEAST(COALESCE((SELECT rn FROM brk) - 1, :max_turns), :max_turns)
       ) AS last_turn
FROM n
""")


async def resolve_chunk(
    session, *, user_id, from_seq: int, upper: int, idle_s: float, max_turns: int
) -> tuple[int, int] | None:
    """이 잡이 먹을 구간 `(from, to]`와 그 구간의 첫 턴. 처리할 게 없으면 None.

    반환 = `(first_turn, last_turn)`. `first_turn`은 하루 경계 판정에 쓴다 — 마지막 턴을 주면
    덩어리 안이 전부 같은 날이라 경계가 안 보인다(검토 지적 C-1).
    """
    row = (await session.execute(_CHUNK_BOUNDS, {
        "user_id": user_id, "from_seq": from_seq, "upper": upper,
        "idle_s": float(idle_s), "max_turns": int(max_turns),
    })).first()
    if row is None or row[0] is None or row[1] is None:
        return None
    return int(row[0]), int(row[1])


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

        generation = int(state.repair_generation)
        from_seq = int(state.ingest_through_turn_seq)
        # 구간은 **첫 시도에서 확정해 payload에 적어둔다.** 매 시도마다 다시 계산하면 그 사이
        # 사용자가 새 턴을 보냈을 때 끝이 밀려, 앞 시도가 남긴 계획을 `_RESUME_PLAN`이 못 찾는다.
        # 그러면 추출을 다시 부르고(결과가 달라진다) 벡터가 중복되고 판정 안 된 기억이 고아로
        # 남는다(검토 지적 H-1).
        frozen = payload.get("chunk")
        if isinstance(frozen, dict) and "first" in frozen and "last" in frozen:
            first_turn, to_seq = int(frozen["first"]), int(frozen["last"])
        else:
            chunk = await resolve_chunk(
                session, user_id=uid, from_seq=from_seq,
                upper=max(turn_seq, int(state.source_through_turn_seq)),
                idle_s=settings.memory_chunk_idle_s,
                max_turns=settings.memory_chunk_max_turns,
            )
            if chunk is None:
                # 커서 뒤에 처리할 턴이 없다 — 커서만 맞추고 끝낸다(잡이 되돌아오지 않게).
                raise JobCancelled("no_source_messages")
            first_turn, to_seq = chunk
            await jobs.freeze_job_payload(
                session, job_id=job.id, patch={"chunk": {"first": first_turn, "last": to_seq}}
            )
        # 아래 단계(staging·registry·커서·판정 잡)는 전부 이 구간의 끝을 기준으로 돈다.
        turn_seq = to_seq

        rows = (await session.execute(
            _SOURCE_MESSAGES, {"user_id": uid, "from_seq": from_seq, "to_seq": to_seq}
        )).all()
        profile = (await session.execute(
            text("SELECT nickname, language FROM profiles WHERE id = :u"), {"u": uid}
        )).first()
        resumed = [
            mem0_pipeline.PlannedCandidate(
                provider_memory_id=r[0], candidate_hash=r[1], text=r[2],
            )
            for r in (await session.execute(_RESUME_PLAN, {
                "user_id": uid, "turn_seq": turn_seq,
                "schema_version": mem0_ingest.SCHEMA_VERSION,
                "generation": generation,
            })).all()
        ]

    if not rows:
        raise JobCancelled("no_source_messages")
    messages = [
        mem0_extractor.SourceMessage(id=r[0], sender=r[1], content=r[2] or "") for r in rows
    ]
    nickname = profile[0] if profile else None
    language = profile[1] if profile else None

    budget = memory_ingest_budget(total_s=settings.job_memory_timeout_s)
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
                    "generation": generation,
                })
                for ev in p.evidence:
                    await session.execute(_CANDIDATE_SOURCE, {
                        "user_id": uid, "provider_memory_id": p.provider_memory_id,
                        "message_id": ev.message_id, "sender": ev.sender,
                        "content_hash": ev.content_hash,
                        "start_utf8": ev.start_utf8, "end_utf8": ev.end_utf8,
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
                for ev in p.evidence:
                    await session.execute(_MEMORY_SOURCE, {
                        "user_id": uid, "provider_memory_id": p.provider_memory_id,
                        "turn_seq": turn_seq, "message_id": ev.message_id,
                        "sender": ev.sender, "content_hash": ev.content_hash,
                        "start_utf8": ev.start_utf8, "end_utf8": ev.end_utf8,
                        "extractor_version": mem0_extractor.EXTRACTOR_VERSION,
                    })
                await session.execute(_COMMIT_CANDIDATE, {
                    "user_id": uid, "provider_memory_id": p.provider_memory_id,
                })
            await session.commit()

    try:
        outcome = await mem0_pipeline.run_ingest(
            user_id=uid, turn_seq=turn_seq, collection_version=COLLECTION_VERSION,
            budget=budget, extract=_extract, embed=_embed, upsert=_upsert,
            stage_planned=_stage, register_pending=_register,
            nickname=nickname, existing_plan=resumed or None,
            source_texts={m.id: m.content for m in messages},
            # 후보 상한을 덩어리 크기에 맞춘다 — 20턴에서 5개만 남기면 뒷부분이 조용히 잘린다.
            turns=to_seq - from_seq,
            repair_generation=generation,
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
        # 판정 대상 유무는 **DB 상태로** 본다. 이번 실행의 outcome만 보면, 앞 시도가 남긴
        # pending을 못 보고 커서를 통과시켜 그 기억이 영원히 판정되지 않는다(실측 사고).
        pending = int((await session.execute(
            _PENDING_FOR_TURN, {"user_id": uid, "turn_seq": turn_seq}
        )).scalar_one() or 0)
        if pending:
            await memory_pipeline.enqueue_consolidate(
                session, uid, turn_seq=turn_seq, privacy_epoch=state.privacy_epoch
            )
        else:
            # 기억 0건인 turn은 판정할 게 없다. consolidate 잡을 만들지 않으므로 여기서
            # 커서를 직접 통과시킨다 — 안 그러면 consolidated 커서가 그 turn에 영원히 걸려
            # cutover gate(`consolidated == ingest`)를 절대 통과할 수 없다.
            await memory_pipeline.advance_consolidated_cursor(session, uid, turn_seq=turn_seq)
        await memory_pipeline.enqueue_next_ingest(
            session, uid, cursor=turn_seq, privacy_epoch=state.privacy_epoch,
            generation=generation,
        )
        # 하루가 닫혔으면 그날의 뒷정리 잡(재판정·관계 투영·계약 추출). 매 turn이 아니라 경계에서만.
        #
        # ⚠️ **덩어리의 끝이 아니라 첫 턴**을 넘긴다. 판정이 "이 턴과 바로 앞 턴의 날짜가 다른가"라서
        #    끝을 주면 덩어리 안이 전부 같은 날이라 경계가 안 보인다. 덩어리는 활동일을 넘지 않게
        #    잘리므로 경계는 항상 첫 턴 앞에 하나만 있다(검토 지적 C-1).
        await memory_pipeline.enqueue_day_boundary_jobs(
            session, uid, turn_seq=first_turn, privacy_epoch=state.privacy_epoch
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

# ⚠️ 여기는 **세대로 거르지 않는다.** provider id에 이미 세대가 들어 있어서 세대별로 갈리므로
# 거를 필요가 없고, 거르면 지난 세대가 남긴 미판정 기억의 본문을 못 찾는다. registry에는 세대
# 컬럼이 없어(`load_pending`도 세대를 안 본다) 그 행은 판정 대상에서 빠지고 영원히 `pending`으로
# 남는다 — 운영 전환 관문의 "미판정 0" 조건을 영구히 막는다(검토 지적 3-2).
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
    budget = memory_consolidation_budget(total_s=settings.job_memory_timeout_s)
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
        # non-active로 닫힌 게 있으면 provider 벡터 정리를 건다. 같은 fenced transaction이라
        # lease를 잃은 소비자가 삭제 잡만 흘리지 않는다.
        if any(t.provider_delete_state == "pending" for t in verdict.transitions):
            await memory_pipeline.enqueue_provider_delete(
                session, uid, turn_seq=turn_seq, privacy_epoch=state.privacy_epoch
            )

    return JobResult(
        result_code="ok",
        result_detail={
            "transitions": len(verdict.transitions),
            "ambiguous_components": verdict.ambiguous_components,
        },
        apply_domain=_publish,
    )


consumer.register(JOB_MEM0_CONSOLIDATE, handle_mem0_consolidate)


JOB_MEM0_PROVIDER_DELETE = "mem0_provider_delete"

# non-active로 확정된 뒤에야 지운다(9.4절 6번). semantic 상태가 먼저다.
_DELETE_TARGETS = text("""
SELECT id, provider_memory_id FROM mem0_memory_registry
WHERE user_id = :user_id
  AND provider_delete_state = 'pending'
  AND semantic_status IN ('duplicate','superseded','excluded','rejected_policy')
ORDER BY updated_at
LIMIT :limit
""")

_MARK_DELETED = text("""
UPDATE mem0_memory_registry
SET provider_delete_state = :state, provider_deleted_at = now(), updated_at = now()
WHERE id = :id AND user_id = :user_id AND provider_delete_state = 'pending'
""")


async def handle_mem0_provider_delete(job: ClaimedJob) -> JobResult:
    """non-active 기억의 provider 벡터를 지운다.

    **삭제가 늦거나 실패해도 노출되지 않는다** — search adapter가 semantic 상태로 이미 거른다.
    그래서 이 잡은 정합성이 아니라 **저장 비용**을 위한 것이며, 실패는 재시도로 충분하다.
    """
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id
    limit = int((job.payload or {}).get("limit", 50))

    async with get_sessionmaker()() as session:
        rows = (await session.execute(
            _DELETE_TARGETS, {"user_id": uid, "limit": limit}
        )).all()
    if not rows:
        return JobResult(result_code="nothing_to_delete")

    deleted, failed = [], []
    for registry_id, provider_id in rows:
        try:
            await _adapter().delete([str(provider_id)], timeout=6.0)
            deleted.append(registry_id)
        except Exception as e:  # noqa: BLE001  개별 실패가 나머지를 막지 않는다
            _log.warning("provider 삭제 실패 — registry=%s: %r", registry_id, e)
            failed.append(registry_id)

    async def _apply(session) -> None:
        for rid in deleted:
            await session.execute(_MARK_DELETED, {"id": rid, "user_id": uid, "state": "deleted"})
        for rid in failed:
            await session.execute(_MARK_DELETED, {"id": rid, "user_id": uid, "state": "failed"})

    return JobResult(
        result_code="ok",
        result_detail={"deleted": len(deleted), "failed": len(failed)},
        apply_domain=_apply,
    )


consumer.register(JOB_MEM0_PROVIDER_DELETE, handle_mem0_provider_delete)
