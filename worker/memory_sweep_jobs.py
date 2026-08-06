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
import uuid

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import diary_recall_repo, jobs, memory_pipeline
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobResult

_log = logging.getLogger("moly-worker")

JOB_MEMORY_SWEEP = memory_pipeline.JOB_MEMORY_SWEEP

# replay operation_id를 원본 job_id에서 결정적으로 만든다 — 같은 잡을 두 번 훑어도 한 번만 산다.
_REPLAY_NAMESPACE = uuid.UUID("6f9b1d2e-0000-4000-8000-000000000001")

# 한 번에 되살릴 사용자 수. 크게 잡으면 한 틱이 길어지고, 작으면 회복이 느리다.
SWEEP_LIMIT = 50

# 죽은 채로 아직 replay되지 않은 기억 잡. 커서가 뒤처진 사용자로 한정한다 — 뒤처지지 않았다면
# 그 잡이 죽었어도 체인은 이미 지나갔으므로 되살릴 이유가 없다.
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

_MISSING_EMBEDDING = text("""
SELECT user_id, diary_id, embedding_repair_attempts
FROM diary_recall_documents
WHERE embedding IS NULL
  AND embedding_repair_attempts < :max_attempts
ORDER BY updated_at
LIMIT :limit
""")

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
_BUMP_REPAIR = text("""
UPDATE diary_recall_documents
SET embedding_repair_attempts = embedding_repair_attempts + 1, updated_at = now()
WHERE user_id = :user_id AND diary_id = :diary_id AND embedding IS NULL
  AND embedding_repair_attempts < :max_attempts
RETURNING embedding_repair_attempts
""")


async def handle_memory_sweep(job: ClaimedJob) -> JobResult:
    """멈춘 사용자에게 다음 turn 잡을 다시 걸고, 비어 있는 일기 임베딩을 다시 만든다. 멱등이다."""
    async with get_sessionmaker()() as session:
        rows = (await session.execute(_STALLED, {"limit": SWEEP_LIMIT})).all()
        missing = (
            await session.execute(
                _MISSING_EMBEDDING,
                {"limit": SWEEP_LIMIT, "max_attempts": MAX_EMBEDDING_REPAIR},
            )
        ).all()
        undocumented = (
            await session.execute(_MISSING_DOCUMENT, {"limit": SWEEP_LIMIT})
        ).all()

    if not rows and not missing and not undocumented:
        return JobResult(result_code="nothing_stalled")

    if rows:
        _log.warning("기억 파이프라인 정지 감지 — 죽은 잡 %s건 replay 시도", len(rows))
    if missing:
        _log.warning("일기 임베딩 누락 %s건 — 다시 만든다", len(missing))
    if undocumented:
        _log.warning("회상 색인에 없는 일기 %s건 — 문서를 만든다", len(undocumented))
    revived: list[str] = []
    repaired = 0
    indexed = 0

    async def _apply(session) -> None:
        nonlocal repaired, indexed
        for job_id, user_id in rows:
            # operation_id가 replay의 멱등 키다. 같은 잡을 두 번 훑어도 새 잡이 겹치지 않게
            # 원본 job_id에서 결정적으로 만든다.
            new_id = await jobs.replay_dead(
                session,
                job_id=job_id,
                operation_id=uuid.uuid5(_REPLAY_NAMESPACE, str(job_id)),
            )
            if new_id is not None:
                revived.append(str(user_id))

        for user_id, diary_id, _attempts in missing:
            attempt = await session.scalar(
                _BUMP_REPAIR,
                {
                    "user_id": user_id,
                    "diary_id": diary_id,
                    "max_attempts": MAX_EMBEDDING_REPAIR,
                },
            )
            if attempt is None:
                continue  # 그 사이 채워졌거나 상한에 닿았다
            # 원래 잡과 같은 중복 방지 키를 쓰면 죽은 잡에 막혀 다시 걸리지 않는다.
            # 회차를 키에 넣어 매번 새 잡이 되게 한다.
            await jobs.enqueue(
                session,
                queue=jobs.QUEUE_CONTENT,
                job_type=diary_recall_repo.JOB_DIARY_RECALL_EMBED,
                user_id=user_id,
                dedup_key=f"diaryembed:repair:{diary_id}:{attempt}",
                payload={
                    "schema_version": diary_recall_repo.INDEX_VERSION,
                    "diary_id": str(diary_id),
                },
            )
            repaired += 1

        for user_id, diary_id in undocumented:
            # 문서 생성은 upsert가 한다. 같은 조건을 두 곳에 두지 않으려고 그 함수를 그대로
            # 부른다. 성공하면 임베딩 잡도 그 안에서 함께 걸린다.
            await diary_recall_repo.upsert_diary_recall_document(
                session, user_id=user_id, diary_id=diary_id
            )
            indexed += 1

    return JobResult(
        result_code="ok",
        result_detail={
            "dead_found": len(rows),
            "revived": len(revived),
            "missing_embedding": len(missing),
            "repair_enqueued": repaired,
            "missing_document": len(undocumented),
            "document_created": indexed,
        },
        apply_domain=_apply,
    )


consumer.register(JOB_MEMORY_SWEEP, handle_memory_sweep)
