"""shadow 프롬프트 계측 잡 — 새 assembler를 응답에 쓰지 않고 재기만 한다(15장 9번).

**live chat 경로를 건드리지 않는다.** 대화가 끝난 뒤 배치로 같은 재료를 다시 모아 v2 프롬프트를
조립하고 크기·캐시 구조만 기록한다. 그래서 이 잡이 실패해도 사용자는 아무 영향을 받지 않는다.

`scripts/verify_prompt_cache.py`는 합성 프리픽스로 캐시가 걸리는 걸 확인했다. 여기서 재는 건
다른 질문이다 — **실제 대화의 길이 분포에서도** 캐시 가능 비율이 설계가 가정한 만큼 나오는가.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import memory_pipeline, prompt_assembly as pa, prompt_trace
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobCancelled, JobFatal, JobResult

_log = logging.getLogger("moly-worker")

JOB_SHADOW_TRACE = "shadow_prompt_trace"

# 조립 규칙이 바뀌면 옛 trace와 섞어 비교하면 안 된다.
ASSEMBLER_VERSION = "v1"

# 최근 원문 창. 실제 chat이 붙이는 만큼과 같아야 계측이 의미 있다.
_RECENT_TURNS = 12

_RECENT = text("""
SELECT sender, content
FROM messages
WHERE user_id = :user_id AND kind = 'normal' AND turn_seq IS NOT NULL
  AND turn_seq <= :turn_seq AND turn_seq > :floor
ORDER BY turn_seq, id
""")

_MEMORIES = text("""
SELECT v.metadata->>'text' AS t
FROM vecs.moly_memories_v2 v
JOIN mem0_memory_registry r ON r.provider_memory_id::text = v.id
WHERE r.user_id = :user_id AND r.semantic_status IN ('active','ambiguous')
ORDER BY r.source_turn_seq DESC
LIMIT 20
""")

_PROFILE = text("SELECT nickname, language FROM profiles WHERE id = :user_id")

_RECORD = text("""
INSERT INTO shadow_prompt_traces
  (user_id, turn_seq, assembler_version, total_bytes, cacheable_bytes,
   volatile_bytes, message_count, segment_counts)
VALUES (:user_id, :turn_seq, :version, :total, :cacheable, :volatile, :msgs, CAST(:counts AS jsonb))
ON CONFLICT (user_id, turn_seq, assembler_version) DO NOTHING
""")


async def handle_shadow_trace(job: ClaimedJob) -> JobResult:
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
        rows = (await session.execute(_RECENT, {
            "user_id": uid, "turn_seq": turn_seq, "floor": max(0, turn_seq - _RECENT_TURNS),
        })).all()
        memories = [r[0] for r in (await session.execute(_MEMORIES, {"user_id": uid})).all() if r[0]]
        profile = (await session.execute(_PROFILE, {"user_id": uid})).first()
    nickname = profile[0] if profile else None
    language = (profile[1] if profile else None) or "ko"

    if not rows:
        raise JobCancelled("no_source_messages")

    segments = _build_segments(rows, memories, nickname, language)
    try:
        ordered = pa.assemble(segments)
    except pa.PromptOrderError as e:
        # 조립 계약 위반은 재시도해도 같다. 계측을 못 할 뿐 대화에는 영향이 없다.
        raise JobFatal("assembly_contract") from e

    t = prompt_trace.trace(ordered)
    counts: dict[str, int] = {}
    for s in ordered:
        counts[s.kind.value] = counts.get(s.kind.value, 0) + 1

    async def _record(session) -> None:
        await session.execute(_RECORD, {
            "user_id": uid, "turn_seq": turn_seq, "version": ASSEMBLER_VERSION,
            "total": t.total_bytes, "cacheable": t.cacheable_bytes,
            "volatile": t.volatile_bytes, "msgs": t.message_count,
            "counts": _json(counts),
        })

    return JobResult(
        result_code="ok",
        result_detail={
            "total_bytes": t.total_bytes,
            "cacheable_ratio": round(t.cacheable_ratio, 3),
        },
        apply_domain=_record,
    )


def _json(d: dict[str, int]) -> str:
    import json

    return json.dumps(d, ensure_ascii=False)


def _build_segments(
    rows, memories: list[str], nickname: str | None, language: str
) -> list[pa.PromptSegment]:
    """실제 chat이 쓰는 재료를 같은 분류로 담는다.

페르소나는 `prompts.system_prompt()`를 **그대로** 쓴다. 여기서 다시 적으면 코드가 단일
    소스라는 규칙이 깨지고 과거 "Molly" 오염 사고가 반복된다.
    """
    from app.services import prompts

    segs = [
        pa.PromptSegment(pa.SegmentKind.PERSONA, "system", prompts.system_prompt(language)),
    ]
    for sender, content in rows:
        segs.append(pa.PromptSegment(
            pa.SegmentKind.RECENT,
            "user" if sender == "user" else "assistant",
            content or "",
        ))
    if memories:
        segs.append(pa.PromptSegment(
            pa.SegmentKind.MEMORY, "system", "\n".join(f"- {m}" for m in memories)
        ))
    if nickname:
        segs.append(pa.PromptSegment(
            pa.SegmentKind.SERVER_SNAPSHOT, "system", f"[지금] 호칭 {nickname}"
        ))
    # 마지막 사용자 발화가 이번 입력이다.
    last_user = next((c for s, c in reversed(rows) if s == "user"), None)
    if last_user:
        segs.append(pa.PromptSegment(pa.SegmentKind.CURRENT_INPUT, "user", last_user))
    return segs


consumer.register(JOB_SHADOW_TRACE, handle_shadow_trace)
