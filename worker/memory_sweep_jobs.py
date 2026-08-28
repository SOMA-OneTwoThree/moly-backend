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
from datetime import datetime, timezone

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

# 다시 걸기까지 기다리는 시간(분). 채팅이 건 잡은 3분 뒤에 도므로 그보다 넉넉히 둔다.
STALL_GRACE_MIN = 30

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
# 배치(#15a): 대상 전체를 한 UPDATE로 올리고, 실제로 오른 행만 RETURNING으로 받는다.
_BUMP_REPAIR_BATCH = text("""
UPDATE diary_recall_documents d
SET embedding_repair_attempts = d.embedding_repair_attempts + 1, updated_at = now()
FROM unnest(:user_ids ::uuid[], :diary_ids ::uuid[]) AS t(user_id, diary_id)
WHERE d.user_id = t.user_id AND d.diary_id = t.diary_id AND d.embedding IS NULL
  AND d.embedding_repair_attempts < :max_attempts
RETURNING d.user_id, d.diary_id, d.embedding_repair_attempts
""")

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
        undocumented = (
            await session.execute(_MISSING_DOCUMENT, {"limit": SWEEP_LIMIT})
        ).all()
        # #28: failed 잔존 존재 여부 — 있으면 _apply에서 bounded 회수한다(부분 인덱스 조회).
        has_failed_deletes = (
            await session.scalar(text(
                "SELECT 1 FROM mem0_memory_registry WHERE provider_delete_state='failed' LIMIT 1"
            ))
        ) is not None

    if (not rows and not missing and not undocumented and not stalled and not unjudged
            and not has_failed_deletes):
        return JobResult(result_code="nothing_stalled")

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
    resumed = 0
    rejudged = 0

    recovered_deletes = 0

    async def _apply(session) -> None:
        nonlocal repaired, indexed, resumed, rejudged, recovered_deletes
        # 시간 칸을 키에 넣어 한 시간에 한 번만 다시 건다. 매 틱마다 새 잡을 쌓지 않는다.
        bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")

        # 죽은 잡 되살리기(`rows`)가 이미 집어간 사람은 건너뛴다. 둘 다 걸면 같은 구간을
        # 두 번 추출해 LLM 호출이 두 배로 든다(데이터는 결정적 id라 수렴한다).
        # 배치(#15a): 행당 enqueue 왕복 N회 → enqueue_many 1문. 카운트 의미는 동일하다
        # (rowcount = 새로 삽입된 수 = 기존 `made is not None` 합).
        replaying = {u for _j, u in rows}
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
                for user_id, cursor, generation, epoch, next_turn in stalled
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
                        "max_attempts": MAX_EMBEDDING_REPAIR,
                    },
                )
            ).all()  # RETURNING에 없는 행 = 그 사이 채워졌거나 상한에 닿았다
            # 원래 잡과 같은 중복 방지 키를 쓰면 죽은 잡에 막혀 다시 걸리지 않는다.
            # 회차를 키에 넣어 매번 새 잡이 되게 한다.
            repaired += await jobs.enqueue_many(
                session,
                queue=jobs.QUEUE_CONTENT,
                job_type=diary_recall_repo.JOB_DIARY_RECALL_EMBED,
                items=[
                    (
                        user_id,
                        f"diaryembed:repair:{diary_id}:{attempt}",
                        {
                            "schema_version": diary_recall_repo.INDEX_VERSION,
                            "diary_id": str(diary_id),
                        },
                    )
                    for user_id, diary_id, attempt in bumped
                ],
            )

        for user_id, diary_id in undocumented:
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
            "stalled_users": len(stalled),
            "resumed": resumed,
            "unjudged_users": len(unjudged),
            "rejudged": rejudged,
            "dead_found": len(rows),
            "revived": len(revived),
            "missing_embedding": len(missing),
            "repair_enqueued": repaired,
            "missing_document": len(undocumented),
            "document_created": indexed,
            "has_failed_deletes": has_failed_deletes,
        },
        apply_domain=_apply,
    )


consumer.register(JOB_MEMORY_SWEEP, handle_memory_sweep)
