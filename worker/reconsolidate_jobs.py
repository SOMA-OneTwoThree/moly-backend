"""이미 살아 있는 기억끼리 다시 판정한다 (9.4절 보수).

**일반 consolidation은 신규 후보만 판정한다.** 새로 들어온 기억을 기존 pool과 비교하고 끝이다.
그래서 판정기 규칙이 나아져도 **이미 active로 굳은 것들은 그대로 남는다** — dev에서
"연락하기를 추가하려고 한다"와 "추가하고 싶다"가 둘 다 active로 남아 프롬프트에 두 번
들어갔다(감사 지적).

이 잡은 그 잔여를 정리한다. 살아 있는 기억을 **서로** 비교해 중복·대체를 닫는다.

⚠️ 새로 만들지 않는다. 상태 전이만 한다 — provider 벡터도 registry 행도 추가되지 않는다.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import mem0_classifier, mem0_consolidation, mem0_registry_repo
from app.services import jobs, memory_pipeline, usage_ledger
from app.services import llm
from app.config import settings
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobFatal, JobResult, JobRetry

_log = logging.getLogger("moly-worker")

JOB_RECONSOLIDATE = "mem0_reconsolidate"

# 한 번에 비교할 기억 수. 쌍 비교라 n²로 늘어나므로 작게 잡는다.
_MAX_ITEMS = 30

_LIVING = text("""
SELECT r.id, r.source_turn_seq, r.content_hash,
       COALESCE(s.source_occurred_at, r.created_at) AS occurred_at,
       v.metadata->>'text' AS body
FROM mem0_memory_registry r
LEFT JOIN LATERAL (
  SELECT MAX(source_occurred_at) AS source_occurred_at
  FROM mem0_memory_sources ms WHERE ms.registry_id = r.id
) s ON true
LEFT JOIN vecs.moly_memories_v2 v ON v.id = r.provider_memory_id::text
WHERE r.user_id = :user_id
  AND r.semantic_status IN ('active', 'ambiguous')
  AND r.classification_version <> :classifier_version
ORDER BY r.source_turn_seq
LIMIT :limit
""")

_REVISION = text(
    "SELECT revision FROM memory_pipeline_states WHERE user_id = :user_id"
)


async def handle_reconsolidate(job: ClaimedJob) -> JobResult:
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id

    async with get_sessionmaker()() as session:
        rows = (await session.execute(_LIVING, {
            "user_id": uid,
            "classifier_version": mem0_classifier.CLASSIFIER_VERSION,
            "limit": _MAX_ITEMS,
        })).all()
        expected_revision = await session.scalar(_REVISION, {"user_id": uid})
        privacy_epoch = (await memory_pipeline.load(session, uid)).privacy_epoch

    items = [(r[0], r[4]) for r in rows if r[4]]
    if len(items) < 2:
        # 비교할 상대가 없으면 판정할 것도 없다.
        return JobResult(result_code="nothing_to_compare")

    # ── 외부 호출: DB 커넥션 0 ──
    try:
        raw = await llm.generate(
            mem0_classifier.build_system(),
            [{"role": "user", "content": mem0_classifier.render_pairs(items, [])}],
            model=settings.model_utility,
            max_tokens=mem0_classifier.MAX_OUTPUT_TOKENS,
            ledger=usage_ledger.LedgerContext(
                lane=usage_ledger.LANE_BACKGROUND, purpose="memory_consolidate",
                user_id=uid, job_id=job.id, attempt=job.attempt,
            ),
        )
        edges = mem0_classifier.parse(raw.text, known_ids={i for i, _ in items})
    except mem0_classifier.ClassifierSchemaError as e:
        raise JobRetry("classifier_schema") from e
    except Exception as e:  # noqa: BLE001
        raise JobRetry("classify_failed") from e

    # 전부 기존 기억이므로 is_new=False다 — canonical 선택이 발생 시각을 따른다.
    refs = [
        mem0_consolidation.MemoryRef(
            registry_id=r[0], source_turn_seq=r[1], candidate_hash=r[2],
            source_occurred_at=r[3], is_new=False,
        )
        for r in rows if r[4]
    ]
    verdict = mem0_consolidation.consolidate(refs, edges)
    if verdict.rejected_reasons:
        _log.warning("재판정 graph 거부 %s — job=%s", verdict.rejected_reasons, job.id)

    async def _apply(session) -> None:
        await mem0_registry_repo.apply_transitions(
            session, uid, verdict.transitions,
            expected_revision=expected_revision,
            classification_version=mem0_classifier.CLASSIFIER_VERSION,
        )
        # non-active로 닫은 게 있으면 provider 벡터 정리를 건다 — 일반 consolidation과 같다
        # (worker/mem0_jobs.py). 이게 빠져 있어서 재판정으로 닫힌 기억의 벡터가 provider에
        # 영영 남았다(dev 실측: `provider_delete_state='pending'` 1건이 재판정 직후 생겨
        # 누구도 집어가지 않았고, cutover 게이트가 그 backlog로 막혔다).
        #
        # dedup key에 **이 잡의 id**를 넣는다. `enqueue`의 ON CONFLICT는 상태와 무관하게
        # 영구적이라, 고정 키를 쓰면 두 번째 재판정부터는 삭제 잡이 조용히 사라진다.
        if any(t.provider_delete_state == "pending" for t in verdict.transitions):
            await jobs.enqueue(
                session,
                queue=jobs.QUEUE_MAINTENANCE,
                job_type=memory_pipeline.JOB_MEM0_PROVIDER_DELETE,
                user_id=uid,
                dedup_key=f"recon-del:{job.id}",
                payload={"turn_seq": 0, "privacy_epoch": privacy_epoch, "limit": 50},
            )

    closed = sum(
        1 for t in verdict.transitions
        if t.semantic_status in ("duplicate", "superseded")
    )
    return JobResult(
        result_code="ok",
        result_detail={"compared": len(items), "closed": closed},
        apply_domain=_apply,
    )


consumer.register(JOB_RECONSOLIDATE, handle_reconsolidate)
