"""checkpoint v2 shadow 생성 — window 체인과 daily digest를 새 계약으로 만든다(15장 8번).

`checkpoint_v2` 모듈은 범위·체인 계약을 갖고 있었지만 **부르는 곳이 없었다.** 그래서 shadow
모드에서 비교할 산출물이 생기지 않았다.

**live 프롬프트를 바꾸지 않는다.** 전부 `publish_state='ready'`로 넣는다. 8.3절이 그 상태를
정확히 이 용도로 정의한다 — 잡이 끝나도 anchor를 줄이지 않고, hard threshold에서 CAS로만
활성화한다. 여기서는 활성화하지 않는다.

요약은 기존 `checkpoint.summarize()`를 그대로 쓴다. 실명 누출 검사·페르소나·마스킹이 거기
붙어 있어서, 별도 경로를 만들면 그 보호가 빠진다.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import checkpoint, checkpoint_v2 as cv2, memory_pipeline, usage_ledger
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobCancelled, JobFatal, JobResult, JobRetry

_log = logging.getLogger("moly-worker")

JOB_SHADOW_CHECKPOINT = "shadow_checkpoint"

# 한 번에 요약할 최대 원문 수. 넘으면 앞이 잘려 부분 이력을 전체인 양 요약하게 된다.
_MAX_SOURCE = 200

_DAY_MESSAGES = text("""
SELECT id, sender, kind, content, created_at
FROM messages
WHERE user_id = :user_id AND activity_date = :day AND kind IN ('normal','greeting')
ORDER BY id
LIMIT :limit
""")

_SEGMENT_AFTER = text("""
SELECT id, sender, kind, content, created_at
FROM messages
WHERE user_id = :user_id AND id > :after AND kind IN ('normal','greeting')
ORDER BY id
LIMIT :limit
""")

# window 체인의 직전 고리. ready도 포함한다 — shadow 체인은 published가 되지 않는다.
_PREV_WINDOW = text("""
SELECT id, summary, segment_from_message_id, segment_through_message_id,
       coverage_from_message_id, coverage_through_message_id
FROM conversation_checkpoints
WHERE user_id = :user_id AND kind = 'window'
  AND coverage_through_message_id IS NOT NULL
ORDER BY coverage_through_message_id DESC
LIMIT 1
""")

_PROFILE = text("SELECT nickname, language FROM profiles WHERE id = :user_id")

_INSERT = text("""
INSERT INTO conversation_checkpoints
  (user_id, through_message_id, summary, source_hash, kind,
   segment_from_message_id, segment_through_message_id,
   coverage_from_message_id, coverage_through_message_id,
   previous_checkpoint_id, locale, source_started_at, source_ended_at,
   activity_date_from, activity_date_to, publish_state)
VALUES
  (:user_id, :through, :summary, :source_hash, :kind,
   :seg_from, :seg_through, :cov_from, :cov_through,
   :prev, :locale, :started, :ended, :day_from, :day_to, 'ready')
ON CONFLICT DO NOTHING
""")


async def handle_shadow_checkpoint(job: ClaimedJob) -> JobResult:
    payload = job.payload or {}
    kind = payload.get("kind", cv2.KIND_DAILY_DIGEST)
    if kind not in (cv2.KIND_WINDOW, cv2.KIND_DAILY_DIGEST):
        raise JobFatal("invalid_kind")
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id

    day: date | None = None
    if kind == cv2.KIND_DAILY_DIGEST:
        try:
            day = date.fromisoformat(payload["activity_date"])
        except (KeyError, TypeError, ValueError) as e:
            raise JobFatal("invalid_payload") from e

    async with get_sessionmaker()() as session:
        state = await memory_pipeline.load(session, uid)
        if not state.records_v2:
            raise JobCancelled("not_v2_user")
        profile = (await session.execute(_PROFILE, {"user_id": uid})).first()
        prev = None
        if kind == cv2.KIND_WINDOW:
            prev = (await session.execute(_PREV_WINDOW, {"user_id": uid})).first()
            after = prev[5] if prev is not None else 0
            rows = (await session.execute(_SEGMENT_AFTER, {
                "user_id": uid, "after": after, "limit": _MAX_SOURCE,
            })).all()
        else:
            rows = (await session.execute(_DAY_MESSAGES, {
                "user_id": uid, "day": day, "limit": _MAX_SOURCE,
            })).all()

    if not rows:
        raise JobCancelled("no_source_messages")

    nickname = profile[0] if profile else None
    language = profile[1] if profile else None
    messages = [
        checkpoint.SourceMessage(id=r[0], sender=r[1], kind=r[2], content=r[3] or "")
        for r in rows
    ]
    seg_from, seg_through = messages[0].id, messages[-1].id

    if kind == cv2.KIND_WINDOW:
        previous_ranges = (
            cv2.Ranges(prev[2], prev[3], prev[4], prev[5]) if prev is not None else None
        )
        try:
            ranges = cv2.window_ranges(
                segment_from=seg_from, segment_through=seg_through, previous=previous_ranges
            )
        except cv2.CheckpointContractError as e:
            # 범위 계약 위반은 재시도해도 같다. 조용히 보정하면 대화 구간이 사라진다.
            raise JobFatal("range_contract") from e
        previous_summary = prev[1] if prev is not None else None
        prev_id = str(prev[0]) if prev is not None else None
    else:
        ranges = cv2.digest_ranges(segment_from=seg_from, segment_through=seg_through)
        previous_summary, prev_id = None, None

    # ── DB 커넥션 0 (LLM 구간) ──
    try:
        summary, _call = await checkpoint.summarize(
            messages=messages,
            previous_summary=previous_summary,
            language=language,
            nickname=nickname,
            ledger=usage_ledger.LedgerContext(
                lane=usage_ledger.LANE_BACKGROUND, purpose="context_summary",
                user_id=uid, job_id=job.id, attempt=job.attempt,
            ),
        )
    except checkpoint.NameLeakError as e:
        _log.warning("shadow 요약 실명 회귀(재시도) — job=%s", job.id)
        raise JobRetry("summary_name_leak") from e
    except checkpoint.CheckpointError as e:
        raise JobRetry("summary_invalid") from e
    except Exception as e:  # noqa: BLE001  provider 장애 — backoff 재시도
        raise JobRetry("summary_llm_failed") from e

    row = cv2.CheckpointRow(
        kind=kind, ranges=ranges, previous_checkpoint_id=prev_id, summary=summary,
        source_hash=checkpoint.source_hash(previous=None, messages=messages),
        locale=language, source_started_at=rows[0][4], source_ended_at=rows[-1][4],
        activity_date_from=day, activity_date_to=day,
        publish_state=cv2.PUBLISH_READY,
    )
    try:
        cv2.validate(row)
    except cv2.CheckpointContractError as e:
        raise JobFatal("checkpoint_contract") from e

    async def _apply(session) -> None:
        await session.execute(_INSERT, {
            "user_id": uid, "through": ranges.coverage_through, "summary": summary,
            "source_hash": row.source_hash, "kind": kind,
            "seg_from": ranges.segment_from, "seg_through": ranges.segment_through,
            "cov_from": ranges.coverage_from, "cov_through": ranges.coverage_through,
            "prev": prev_id, "locale": language,
            "started": row.source_started_at, "ended": row.source_ended_at,
            "day_from": day, "day_to": day,
        })

    return JobResult(
        result_code="ok",
        result_detail={"kind": kind, "coverage_through": ranges.coverage_through},
        apply_domain=_apply,
    )


consumer.register(JOB_SHADOW_CHECKPOINT, handle_shadow_checkpoint)
