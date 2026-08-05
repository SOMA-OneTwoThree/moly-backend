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
from app.services import jobs, memory_pipeline
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


async def handle_memory_sweep(job: ClaimedJob) -> JobResult:
    """멈춘 사용자에게 다음 turn 잡을 다시 건다. 멱등이다."""
    async with get_sessionmaker()() as session:
        rows = (await session.execute(_STALLED, {"limit": SWEEP_LIMIT})).all()

    if not rows:
        return JobResult(result_code="nothing_stalled")

    _log.warning("기억 파이프라인 정지 감지 — 죽은 잡 %s건 replay 시도", len(rows))
    revived: list[str] = []

    async def _apply(session) -> None:
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

    return JobResult(
        result_code="ok",
        result_detail={"dead_found": len(rows), "revived": len(revived)},
        apply_domain=_apply,
    )


consumer.register(JOB_MEMORY_SWEEP, handle_memory_sweep)
