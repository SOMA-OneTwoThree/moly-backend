"""기억 잡 핸들러 — 추출 → 판정 → 임베딩·관계 프로필 투영.

W7 계약 위에서 지키는 것(어기면 조용히 깨진다):

1. **도메인 쓰기와 후속 enqueue는 fenced finalize와 같은 트랜잭션**(`JobResult.apply_domain`)에서만
   한다. lease를 잃었으면 `jobs.finalize_success`가 apply_domain을 아예 부르지 않으므로,
   늦게 돌아온 소비자가 기억을 두 번 반영하거나 후속 잡을 다시 만드는 일이 없다.
2. **LLM 호출 중에는 DB 세션을 쥐지 않는다.** 읽기용 짧은 세션을 먼저 닫고 호출한다(SOMA-374와
   같은 불변식 — 여기선 워커라 커넥션 풀 고갈이 문제다).
3. **판정은 LLM이 하지 않는다.** 후보는 `memory_extract`, 판정은 `memory_reconcile.decide`,
   반영은 `memory_repo.apply_decisions`다. 이 파일은 순서와 트랜잭션 경계만 책임진다.
4. closure 겹침·stale generation은 결과를 버리고 **succeeded + 사유 코드**로 끝낸다(명세 §W8:
   "어긋나면 결과를 버리고 succeeded + 사유 코드"). 재시도해도 같은 결과라 실패로 두지 않는다.
5. 실패 분류: 잡 payload 위반 = `JobFatal`(결정적, 재시도 무의미) / 모델 출력 스키마 위반 =
   `JobRetry`(같은 입력이라도 다음 샘플은 통과할 수 있다, attempt 소진 시 dead+경보).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.db import get_sessionmaker
from app.services import (
    memory_candidates,
    memory_extract,
    memory_embeddings,
    memory_norm,
    memory_reconcile,
    memory_repo,
    relationship_profile,
    relationship_profile_repo,
)
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobCancelled, JobFatal, JobResult, JobRetry

_log = logging.getLogger("moly-worker")

# finalize 사유 코드(계약) — 대시보드/쿼리가 이 값을 본다.
RESULT_OK = "ok"
RESULT_STALE_GENERATION = "stale_generation"
RESULT_NO_CANDIDATES = "no_candidates"
RESULT_PROFILE_UNCHANGED = "profile_unchanged"

# 경보 코드(= alert dedup key의 일부).
ERROR_UNSUPPORTED_VERSION = "unsupported_normalization_version"
ERROR_RECONCILE_CONFLICT = "reconcile_conflict"
ERROR_INVALID_CANDIDATE_PAYLOAD = "invalid_candidate_payload"

_STATE_SQL = text("""
SELECT p.nickname, p.language, COALESCE(c.memory_generation, 0) AS memory_generation
FROM profiles p LEFT JOIN chat_contexts c ON c.user_id = p.id
WHERE p.id = :user_id
""")


@dataclass(frozen=True, slots=True)
class _Source:
    """잡 payload에서 꺼낸 소스 좌표. 여기까지 왔으면 형식은 이미 검증됐다."""

    user_id: uuid.UUID
    memory_generation: int
    from_watermark: int
    through_watermark: int
    message_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _UserState:
    nickname: str | None
    language: str | None
    memory_generation: int


# ─────────────────────────────────────────────────────────────
# payload 검증 — 어기면 재시도해도 같으므로 즉시 dead.
# ─────────────────────────────────────────────────────────────
def _int_field(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise JobFatal("invalid_payload")
    return value


def _parse_source(job: ClaimedJob) -> _Source:
    payload = job.payload if isinstance(job.payload, dict) else {}
    if payload.get("schema_version") != memory_repo.SOURCE_PAYLOAD_SCHEMA_VERSION:
        raise JobFatal("unsupported_payload_schema")
    if payload.get("source_kind") != memory_repo.SOURCE_KIND_CONVERSATION_TURN:
        # v1 소스는 conversation_turn뿐이다(§W8). 모르는 소스를 흡수하면 closure 계약이 깨진다.
        raise JobFatal("unsupported_source_kind")
    if job.user_id is None:
        raise JobFatal("invalid_payload")
    from_wm = _int_field(payload, "source_from_watermark")
    through_wm = _int_field(payload, "source_through_watermark")
    if from_wm < 1 or through_wm < from_wm:
        raise JobFatal("invalid_payload")
    raw_ids = payload.get("message_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise JobFatal("invalid_payload")
    ids: list[int] = []
    for raw in raw_ids:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0 or raw in ids:
            raise JobFatal("invalid_payload")
        ids.append(raw)
    return _Source(
        user_id=job.user_id,
        memory_generation=_int_field(payload, "memory_generation"),
        from_watermark=from_wm,
        through_watermark=through_wm,
        message_ids=tuple(ids),
    )


# ─────────────────────────────────────────────────────────────
# 공통 게이트 — closure 겹침이 먼저, 그다음 generation(명세의 검사 순서).
# ─────────────────────────────────────────────────────────────
async def _load_user_state(session: AsyncSession, user_id: uuid.UUID) -> _UserState:
    row = (await session.execute(_STATE_SQL, {"user_id": user_id})).first()
    if row is None:  # 탈퇴 — 처리 의미가 사라졌다(경보 대상 아님)
        raise JobCancelled("user_deleted")
    return _UserState(nickname=row[0], language=row[1], memory_generation=int(row[2]))


async def _load_state(session: AsyncSession, src: _Source) -> _UserState:
    return await _load_user_state(session, src.user_id)


async def _skip_reason(
    session: AsyncSession, src: _Source, generation: int, *, alert: bool
) -> str | None:
    """버려야 할 이유가 있으면 사유 코드. 없으면 None.

    `alert=False`는 finalize 직전 재검사용이다 — 같은 잡이 같은 사유로 두 번 경보하지 않게.
    """
    closures = await memory_repo.load_closures(
        session,
        src.user_id,
        from_watermark=src.from_watermark,
        through_watermark=src.through_watermark,
    )
    try:
        memory_reconcile.assert_source_range_open(
            closures, src.from_watermark, src.through_watermark
        )
    except memory_reconcile.SourceRangeClosedError as e:
        if alert:
            await memory_reconcile.alert_failure(
                user_id=src.user_id,
                error_code=memory_reconcile.RESULT_SOURCE_RANGE_CLOSED,
                detail=str(e),
            )
        return memory_reconcile.RESULT_SOURCE_RANGE_CLOSED
    if generation != src.memory_generation:
        # forget/cutover가 세대를 올렸다 = 이 결과는 이전 세대의 것이다.
        return RESULT_STALE_GENERATION
    return None


async def _still_applicable(session: AsyncSession, src: _Source) -> bool:
    """fenced finalize 트랜잭션 안에서 하는 마지막 확인(closure·generation을 fresh read).

    어긋나면 도메인 반영도 후속 enqueue도 하지 않는다. 여기서 예외를 던지면 finalize가
    통째로 롤백돼 lease 만료까지 잡이 running으로 남으므로, 던지지 않고 조용히 건너뛴다
    (그 사이 탈퇴한 유저도 같은 이유로 예외 대신 False다).
    """
    try:
        state = await _load_state(session, src)
    except JobCancelled:
        _log.warning("기억 잡 확정 직전 대상 유저 없음 — 반영 생략 job_user=%s", src.user_id)
        return False
    reason = await _skip_reason(session, src, state.memory_generation, alert=False)
    if reason is not None:
        _log.warning(
            "기억 잡 확정 직전 상태 변화로 반영 생략 — user_gen=%s job_gen=%s reason=%s",
            state.memory_generation, src.memory_generation, reason,
        )
        return False
    return True


# ─────────────────────────────────────────────────────────────
# 1) memory_extract — 대화 턴에서 후보 추출(LLM). content 큐.
# ─────────────────────────────────────────────────────────────
async def handle_extract(job: ClaimedJob) -> JobResult:
    src = _parse_source(job)
    async with get_sessionmaker()() as session:
        state = await _load_state(session, src)
        reason = await _skip_reason(session, src, state.memory_generation, alert=True)
        if reason is not None:
            return JobResult(result_code=reason)
        # 대표/근거 메시지 적재는 repo의 검증 경로를 그대로 쓴다 — 타 유저 메시지가 섞이면
        # 여기서 CrossUserEvidenceError로 끊긴다(추출 프롬프트에 남의 대화가 들어갈 수 없다).
        try:
            loaded = await memory_repo.load_turn_messages(session, src.user_id, src.message_ids)
        except memory_repo.CrossUserEvidenceError as e:
            raise JobFatal("invalid_payload") from e
    # ── 여기서부터 DB 커넥션 0 (LLM 구간) ──
    messages = [
        memory_extract.SourceMessage(id=mid, sender=sender, content=content)
        for mid, (sender, content) in sorted(loaded.items())
        if sender == "user"
    ]
    try:
        candidates, _usage = await memory_extract.extract(
            messages=messages, language=state.language, nickname=state.nickname
        )
    except memory_candidates.CandidateSchemaError as e:
        # 모델 출력이 계약을 어겼다 — 후보 전량 폐기 후 재시도(다음 샘플은 통과할 수 있다).
        _log.warning("기억 추출 스키마 실패(재시도) — job_id=%s: %s", job.id, e)
        raise JobRetry("candidate_schema") from e
    except Exception as e:  # noqa: BLE001  # provider 타임아웃·429·일시 장애 — backoff 재시도
        raise JobRetry("extract_llm_failed") from e

    if not candidates:
        return JobResult(result_code=RESULT_NO_CANDIDATES, result_detail={"candidates": 0})

    payload = memory_extract.to_payload(candidates)

    async def _apply(session: AsyncSession) -> None:
        if not await _still_applicable(session, src):
            return
        await memory_repo.enqueue_reconcile(
            session,
            user_id=src.user_id,
            memory_generation=src.memory_generation,
            from_watermark=src.from_watermark,
            through_watermark=src.through_watermark,
            message_ids=src.message_ids,
            candidate_payload=payload,
        )

    return JobResult(
        result_code=RESULT_OK,
        result_detail={"candidates": len(candidates)},
        apply_domain=_apply,
    )


# ─────────────────────────────────────────────────────────────
# 2) memory_reconcile — 코드 판정 + 반영. LLM 호출 없음.
# ─────────────────────────────────────────────────────────────
def _decision_counts(decisions: list[memory_reconcile.Decision]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in decisions:
        out[d.action] = out.get(d.action, 0) + 1
    return out


async def handle_reconcile(job: ClaimedJob) -> JobResult:
    src = _parse_source(job)
    raw_candidates = (job.payload or {}).get("candidate_payload")
    observed_at = datetime.now(timezone.utc)

    async with get_sessionmaker()() as session:
        state = await _load_state(session, src)
        reason = await _skip_reason(session, src, state.memory_generation, alert=True)
        if reason is not None:
            return JobResult(result_code=reason)

        try:
            # payload의 message_ids가 구간의 정확한 합집합인지(닫힌 구간·타 유저 혼입 차단).
            await memory_repo.assert_payload_message_ids(
                session,
                src.user_id,
                from_watermark=src.from_watermark,
                through_watermark=src.through_watermark,
                message_ids=src.message_ids,
            )
            candidates = memory_candidates.parse_candidates(
                raw_candidates,
                allowed_message_ids=src.message_ids,
                nickname=state.nickname,
            )
        except (memory_candidates.CandidateSchemaError, memory_repo.CrossUserEvidenceError) as e:
            # 이미 저장된 payload가 계약을 어겼다 = 재시도해도 같다. publish 0으로 끝낸다.
            await memory_reconcile.alert_failure(
                user_id=src.user_id, error_code=ERROR_INVALID_CANDIDATE_PAYLOAD, detail=str(e)
            )
            raise JobFatal(ERROR_INVALID_CANDIDATE_PAYLOAD) from e

        if not candidates:
            return JobResult(result_code=RESULT_NO_CANDIDATES)

        existing = await memory_repo.load_active_facts(session, src.user_id)
        markers = await memory_repo.load_forget_markers(session, src.user_id)
        watermarks = await memory_repo.watermarks_for_messages(
            session,
            src.user_id,
            [mid for c in candidates for mid in c.evidence_message_ids],
        )
        try:
            decisions = memory_reconcile.decide(
                candidates, existing=existing, markers=markers, watermarks=watermarks
            )
        except memory_norm.UnsupportedNormalizationVersionError as e:
            # marker version을 재계산할 수 없으면 잊은 사실이 되살아난다 — 무음 우회 금지.
            await memory_reconcile.alert_failure(
                user_id=src.user_id, error_code=ERROR_UNSUPPORTED_VERSION, detail=str(e)
            )
            raise JobFatal(ERROR_UNSUPPORTED_VERSION) from e
        except memory_reconcile.ReconcileError as e:
            # 동률 충돌 등 최신성을 정할 수 없는 상태 — 어느 쪽도 publish하지 않는다.
            await memory_reconcile.alert_failure(
                user_id=src.user_id, error_code=ERROR_RECONCILE_CONFLICT, detail=str(e)
            )
            raise JobFatal(ERROR_RECONCILE_CONFLICT) from e

    async def _apply(session: AsyncSession) -> None:
        if not await _still_applicable(session, src):
            return
        result = await memory_repo.apply_decisions(
            session,
            user_id=src.user_id,
            decisions=decisions,
            observed_at=observed_at,
            nickname=state.nickname,
        )
        if result.changed and result.revision is not None:
            # 실제로 바뀐 트랜잭션에서만 프로필 재생성을 건다(revision이 dedup key다).
            await memory_repo.enqueue_profile_refresh(
                session,
                user_id=src.user_id,
                memory_generation=src.memory_generation,
                input_revision=result.revision,
            )
            await memory_repo.enqueue_embedding(
                session,
                user_id=src.user_id,
                memory_generation=src.memory_generation,
                input_revision=result.revision,
            )

    return JobResult(
        result_code=RESULT_OK,
        result_detail=_decision_counts(decisions),
        apply_domain=_apply,
    )


# ─────────────────────────────────────────────────────────────
# 3) memory_embed — active 사실의 pgvector 파생 인덱스.
# ─────────────────────────────────────────────────────────────
def _parse_profile_job(job: ClaimedJob) -> tuple[uuid.UUID, int, int]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    if payload.get("schema_version") != memory_repo.SOURCE_PAYLOAD_SCHEMA_VERSION:
        raise JobFatal("unsupported_payload_schema")
    if job.user_id is None:
        raise JobFatal("invalid_payload")
    generation = _int_field(payload, "memory_generation")
    revision = _int_field(payload, "relationship_profile_input_revision")
    if generation < 0 or revision < 1:
        raise JobFatal("invalid_payload")
    return job.user_id, generation, revision


async def handle_embed(job: ClaimedJob) -> JobResult:
    user_id, generation, revision = _parse_profile_job(job)
    async with get_sessionmaker()() as session:
        state = await relationship_profile_repo.input_coordinates(session, user_id)
        if state != (generation, revision):
            return JobResult(result_code=RESULT_STALE_GENERATION)
        sources = await memory_repo.load_missing_embeddings(session, user_id)
    if not sources:
        return JobResult(result_code=RESULT_OK, result_detail={"embedded": 0})
    try:
        vectors: list[list[float]] = []
        batch_size = settings.memory_embedding_batch_size
        for start in range(0, len(sources), batch_size):
            batch = sources[start:start + batch_size]
            vectors.extend(await memory_embeddings.embed_texts([s.text for s in batch]))
    except Exception as e:  # noqa: BLE001 — provider/네트워크는 bounded retry
        raise JobRetry("embedding_failed") from e

    async def _apply(session: AsyncSession) -> None:
        if await relationship_profile_repo.input_coordinates(session, user_id) != (
            generation,
            revision,
        ):
            return
        await memory_repo.write_embeddings(
            session, user_id, [(source.id, vector) for source, vector in zip(sources, vectors)]
        )

    return JobResult(
        result_code=RESULT_OK,
        result_detail={"embedded": len(sources)},
        apply_domain=_apply,
    )


# ─────────────────────────────────────────────────────────────
# 4) relationship_profile_refresh — active 기억을 안정적인 프롬프트 투영으로 publish.
# ─────────────────────────────────────────────────────────────


def _profile_document(
    sources: list[memory_repo.ProfileSource], nickname: str | None
) -> relationship_profile.ProfileDocument:
    """active 기억을 결정적으로 슬롯에 배치한다. 같은 입력은 같은 render_hash를 만든다."""
    facts = [s for s in sources if s.source_type == relationship_profile.SOURCE_FACT]
    insights = [s for s in sources if s.source_type == relationship_profile.SOURCE_INSIGHT]
    recent_kinds = {"event", "emotion"}

    def _item(s: memory_repo.ProfileSource, prefix: str) -> relationship_profile.ProfileItem:
        return relationship_profile.build_item(
            item_key=f"{prefix}:{s.id}",
            text=s.text,
            source_refs=[{"type": s.source_type, "id": str(s.id)}],
            nickname=nickname,
        )

    known = [_item(s, "known") for s in facts if s.kind not in recent_kinds]
    recent = sorted(
        (s for s in facts if s.kind in recent_kinds),
        key=lambda s: (
            s.event_time is not None,
            s.event_time or datetime.min.replace(tzinfo=timezone.utc),
            s.importance,
        ),
        reverse=True,
    )
    inferred = [_item(s, "inferred") for s in insights]
    return relationship_profile.build_document(
        known_facts=known,
        recent_threads=[_item(s, "recent") for s in recent],
        inferred_tendencies=inferred,
    )


async def handle_profile_refresh(job: ClaimedJob) -> JobResult:
    """현재 generation/revision의 active 기억을 읽고 fenced finalize 안에서 publish한다."""
    user_id, generation, revision = _parse_profile_job(job)
    async with get_sessionmaker()() as session:
        state = await _load_user_state(session, user_id)
        coords = await relationship_profile_repo.input_coordinates(session, user_id)
        if coords != (generation, revision):
            return JobResult(result_code=RESULT_STALE_GENERATION)
        sources = await memory_repo.load_profile_sources(session, user_id)
    document = _profile_document(sources, state.nickname)

    async def _apply(session: AsyncSession) -> None:
        if await relationship_profile_repo.input_coordinates(session, user_id) != (
            generation,
            revision,
        ):
            return
        draft = await relationship_profile_repo.create_draft(
            session,
            user_id=user_id,
            language=state.language,
            document=document,
            nickname=state.nickname,
        )
        if draft.status == relationship_profile_repo.RESULT_UNCHANGED:
            return
        await relationship_profile_repo.publish(
            session,
            user_id=user_id,
            profile_id=draft.profile_id,
            locale=None,
        )

    return JobResult(
        result_code=RESULT_PROFILE_UNCHANGED if document.is_empty else RESULT_OK,
        result_detail={"sources": len(sources), "items": len(document.items)},
        apply_domain=_apply,
    )


# import 시 1회 등록(모듈 캐시 = 중복 등록 없음). consumer가 이 모듈을 지역 import 하는 이유는
# 순환 import 회피 — 여기서 consumer의 JobResult/JobRetry 계약을 쓰기 때문이다.
consumer.register(memory_repo.JOB_MEMORY_EXTRACT, handle_extract)
consumer.register(memory_repo.JOB_MEMORY_RECONCILE, handle_reconcile)
consumer.register(memory_repo.JOB_MEMORY_EMBED, handle_embed)
consumer.register(memory_repo.JOB_PROFILE_REFRESH, handle_profile_refresh)
