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
from app.services.mem0_budget import memory_consolidation_budget
from worker import consumer
from worker.consumer import JobFatal, JobResult, JobRetry

_log = logging.getLogger("moly-worker")

JOB_RECONSOLIDATE = "mem0_reconsolidate"

# 한 번에 비교할 기억 수. 쌍 비교라 n²로 늘어나므로 작게 잡는다.
_MAX_ITEMS = 30

# ⚠️ 대상 선택은 **마지막으로 본 시각** 기준이다.
#
# 예전에는 `classification_version <> '현재 버전'`이었는데, 판정을 마친 기억은 예외 없이 그
# 값이 되므로 조건이 **절대 참이 될 수 없었다.** 운영에서 이 잡 706건이 전부
# `nothing_to_compare`로 끝났다 — 한 번도 돈 적이 없다. `NULL <> 'x'`도 참이 아니라서
# 값이 비어 있는 행까지 빠졌다.
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
  AND (r.last_reconsolidated_at IS NULL
       OR r.last_reconsolidated_at < now() - make_interval(days => :cooldown_days))
ORDER BY r.last_reconsolidated_at NULLS FIRST, r.source_turn_seq
LIMIT :limit
""")

# 비교에 참여한 행에 표시를 남긴다. **전이가 없어도 남긴다** — 안 그러면 커서가 앞으로 못 가고
# 같은 30건만 매일 다시 본다.
_MARK_SEEN = text("""
UPDATE mem0_memory_registry SET last_reconsolidated_at = now()
WHERE user_id = :user_id AND id = ANY(:ids)
""")

# 한 번 본 기억을 다시 보기까지 기다리는 날. 매일 같은 것을 되씹지 않게 한다.
_COOLDOWN_DAYS = 7

_REVISION = text(
    "SELECT revision FROM memory_pipeline_states WHERE user_id = :user_id"
)


async def handle_reconsolidate(job: ClaimedJob) -> JobResult:
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id
    # 일반 판정과 같은 예산을 쓴다. 예산이 없으면 확정(finalize) 몫이 남지 않아 느려질 때
    # 처리 권한을 잃는다.
    budget = memory_consolidation_budget(total_s=settings.job_memory_timeout_s)

    async with get_sessionmaker()() as session:
        rows = (await session.execute(_LIVING, {
            "user_id": uid,
            "cooldown_days": _COOLDOWN_DAYS,
            "limit": _MAX_ITEMS,
        })).all()
        expected_revision = await session.scalar(_REVISION, {"user_id": uid})
        privacy_epoch = (await memory_pipeline.load(session, uid)).privacy_epoch
        lang_row = (await session.execute(
            text("SELECT language FROM profiles WHERE id = :u"), {"u": uid}
        )).first()
        language = lang_row[0] if lang_row else None

    items = [(r[0], r[4]) for r in rows if r[4]]
    if len(items) < 2:
        # 비교할 상대가 없으면 판정할 것도 없다.
        return JobResult(result_code="nothing_to_compare")

    # ── 외부 호출: DB 커넥션 0 ──
    try:
        raw = await llm.generate(
            mem0_classifier.build_system(language),
            [{"role": "user", "content": mem0_classifier.render_pairs(
                items, [], language=language)}],
            model=settings.model_utility,
            max_tokens=mem0_classifier.MAX_OUTPUT_TOKENS,
            timeout=budget.timeout_for("classify"),
            # 유틸리티 모델은 추론 모델이다 — 안 끄면 추론 토큰이 출력 상한을 먹어 답이 잘린다.
            reasoning_effort="none",
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
        # 비교에 참여한 것 전부에 표시를 남긴다. **전이가 없어도 남긴다** — 안 그러면 커서가
        # 앞으로 못 가고 다음 회차가 같은 30건을 또 본다(31번째 이후는 영영 안 본다).
        await session.execute(
            _MARK_SEEN, {"user_id": uid, "ids": [r[0] for r in rows if r[4]]}
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
