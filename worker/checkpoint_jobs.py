"""대화 요약 checkpoint 잡 핸들러(W11) — `conversation_checkpoint`. content 큐.

W7 계약 위에서 지키는 것(어기면 조용히 깨진다):

1. **도메인 쓰기는 fenced finalize와 같은 트랜잭션**(`JobResult.apply_domain`). lease를 잃었으면
   `jobs.finalize_success`가 apply_domain을 아예 부르지 않으므로 늦게 돌아온 소비자가 옛 요약을
   덮어쓰지 않는다(`worker/memory_jobs.py`와 같은 방식).
2. **LLM 호출 중에는 DB 세션을 쥐지 않는다.** 읽기용 짧은 세션을 먼저 닫고 호출한다.
3. **입력이 그대로인지 확인한 뒤에만 요약한다.** 범위 메시지를 다시 읽어 `source_hash`를 재계산하고
   payload와 다르면 버린다(succeeded + 사유 코드) — 그 사이 앞선 checkpoint가 생겼거나 근거가
   달라진 것이고, 재시도해도 같은 결과다.
4. 🔴 **Summary는 Fact가 아니다.** 이 파일은 기억 추출·reconcile 잡을 **만들지 않는다**(§W11-7).
   요약을 사실로 되먹이면 근거가 요약으로 오염되고 W8의 evidence 계약이 깨진다.
5. **킬스위치가 off면 아무 일도 하지 않는다** — 이미 큐에 있던 잡도 쓰기 없이 succeeded로 끝낸다.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.db import get_sessionmaker
from app.services import checkpoint, checkpoint_repo
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobCancelled, JobFatal, JobResult, JobRetry

_log = logging.getLogger("moly-worker")

# finalize 사유 코드(계약) — 대시보드/쿼리가 이 값을 본다.
RESULT_OK = "ok"
RESULT_DISABLED = "checkpoint_disabled"
RESULT_ALREADY_CHECKPOINTED = "already_checkpointed"
RESULT_SOURCE_CHANGED = "source_changed"
# 잡을 만든 뒤 "잊어줘"가 실행됐다 — 그 대화를 다시 요약해 넣으면 안 된다(재시도해도 영원히 stale).
RESULT_STALE_GENERATION = "stale_generation"
# 요약 대상 구간에 잊어줘가 닫은 메시지가 섞여 있다 — 요약하면 지운 내용이 되살아난다.
RESULT_SOURCE_CLOSED = "source_range_closed"


@dataclass(frozen=True, slots=True)
class _Request:
    """잡 payload에서 꺼낸 요약 요청. 여기까지 왔으면 형식은 이미 검증됐다."""

    user_id: uuid.UUID
    through_message_id: int
    source_message_ids: tuple[int, ...]
    source_hash: str
    version: str
    previous_id: str | None
    previous_through_message_id: int | None
    memory_generation: int


def _int_field(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise JobFatal("invalid_payload")
    return value


def _generation_field(payload: dict) -> int:
    """payload의 memory_generation. **0이 정상값**이라 _int_field(양수 강제)를 쓸 수 없다."""
    value = payload.get("memory_generation")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JobFatal("invalid_payload")
    return value


def _parse(job: ClaimedJob) -> _Request:
    """payload 검증 — 어기면 재시도해도 같으므로 즉시 dead."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    if payload.get("schema_version") != checkpoint.SCHEMA_VERSION:
        raise JobFatal("unsupported_payload_schema")
    if job.user_id is None:
        raise JobFatal("invalid_payload")
    raw_ids = payload.get("source_message_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise JobFatal("invalid_payload")
    ids: list[int] = []
    for raw in raw_ids:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0 or raw in ids:
            raise JobFatal("invalid_payload")
        ids.append(raw)
    source_hash = payload.get("source_hash")
    version = payload.get("summarizer_version")
    if not isinstance(source_hash, str) or not source_hash:
        raise JobFatal("invalid_payload")
    if not isinstance(version, str) or not version:
        raise JobFatal("invalid_payload")
    prev_id = payload.get("previous_checkpoint_id")
    if prev_id is not None and not isinstance(prev_id, str):
        raise JobFatal("invalid_payload")
    prev_through = payload.get("previous_through_message_id")
    if prev_through is not None and (
        isinstance(prev_through, bool) or not isinstance(prev_through, int)
    ):
        raise JobFatal("invalid_payload")
    return _Request(
        user_id=job.user_id,
        through_message_id=_int_field(payload, "through_message_id"),
        source_message_ids=tuple(sorted(ids)),
        source_hash=source_hash,
        version=version,
        previous_id=prev_id,
        previous_through_message_id=prev_through,
        memory_generation=_generation_field(payload),
    )


def _chain_matches(previous: checkpoint.Checkpoint | None, req: _Request) -> bool:
    """지금 DB의 최신 checkpoint가 이 잡이 전제한 이전 마디와 같은가.

    다르면(그 사이 다른 checkpoint가 들어왔다) 이 잡의 `source_hash`는 더 이상 성립하지 않는다.
    """
    return (str(previous.id) if previous is not None else None) == req.previous_id


async def _generation_matches(session: AsyncSession, req: _Request) -> bool:
    """잡을 만든 시점과 지금의 기억 세대가 같은가. 다르면 그 사이에 forget이 있었다.

    **이른 종료용이다** — LLM을 괜히 부르지 않으려는 것이고, 최종 방어는 `checkpoint_repo.insert`가
    같은 문장 안에서 하는 세대 검사다(읽고 나서 쓰는 사이의 경합을 잠금 없이 없앤다).
    """
    current = await checkpoint_repo.read_memory_generation(session, req.user_id)
    if current == req.memory_generation:
        return True
    _log.info(
        "요약 잡 stale — user=%s payload_gen=%d current_gen=%d(그 사이 forget)",
        req.user_id, req.memory_generation, current,
    )
    return False


async def _still_applicable(
    session: AsyncSession, req: _Request
) -> tuple[bool, checkpoint.Checkpoint | None]:
    """fenced finalize 트랜잭션 안에서 하는 마지막 확인(최신 checkpoint를 fresh read).

    어긋나면 조용히 건너뛴다 — 여기서 예외를 던지면 finalize가 통째로 롤백돼 lease 만료까지 잡이
    running으로 남는다(memory_jobs와 같은 규칙).
    """
    # 세대부터 본다. LLM 구간(DB 커넥션 0)에서 forget이 끼면 checkpoint가 전량 지워져
    # previous=None이 되는데, 그 상태가 "첫 요약"과 구분이 안 돼 체인 검사만으로는 통과한다.
    if not await _generation_matches(session, req):
        return False, None
    previous = await checkpoint_repo.load_latest(session, req.user_id)
    if previous is not None and previous.through_message_id >= req.through_message_id:
        return False, previous
    if not _chain_matches(previous, req):
        return False, previous
    return True, previous


async def _load_sources(
    session: AsyncSession, req: _Request, previous: checkpoint.Checkpoint | None
) -> list[checkpoint.SourceMessage]:
    """요약 대상 구간을 DB에서 다시 읽는다 — payload의 목록을 그대로 믿지 않는다.

    **하한은 이전 checkpoint가 아니라 payload가 지목한 첫 메시지**다. producer가 넘기는 세그먼트는
    `messages` 전체가 아니라 **앵커 이후**라서, 앵커 리셋을 겪은 유저에겐 앵커 이전 이력이 DB에 그대로
    남아 있다. 하한을 `(이전 checkpoint or 0)`으로 잡으면 그 옛 메시지까지 읽혀 목록이 어긋나고,
    checkpoint가 0건인 유저는 **영원히** `source_changed`로만 끝난다(체인이 시작되지 않는다).

    하한을 payload에서 받아도 신뢰 문제는 없다 — 읽어온 목록으로 `source_hash`를 다시 계산해
    payload와 대조하고, 그 해시에는 이전 checkpoint의 `(id, source_hash)`가 물려 있다. 즉 하한을
    조작해도 유효한 요약으로 성립시킬 수 없다.

    payload id 목록과 다르면(메시지가 지워졌거나 사이에 새 행이 끼었다) 빈 목록을 돌려 호출측이
    `source_changed`로 끝내게 한다.
    """
    from_id = req.source_message_ids[0]
    if previous is not None and from_id <= previous.through_message_id:
        # 이전 checkpoint가 이미 덮은 구간과 겹친다 = 같은 대화를 두 번 요약하게 된다.
        return []
    messages = await checkpoint_repo.load_range(
        session,
        req.user_id,
        after_id=from_id - 1,  # 열린 하한 → `[from_id, through]` 닫힌 구간
        through_id=req.through_message_id,
        max_rows=len(req.source_message_ids) + 1,  # +1 = 초과분(사이에 낀 새 행) 탐지용
    )
    if tuple(sorted(m.id for m in messages)) != req.source_message_ids:
        return []
    return messages


async def handle_checkpoint(job: ClaimedJob) -> JobResult:
    if not settings.context_checkpoint_enabled:  # 킬스위치 — 쓰기도 LLM 호출도 없다
        return JobResult(result_code=RESULT_DISABLED)
    req = _parse(job)

    async with get_sessionmaker()() as session:
        state = await checkpoint_repo.load_user_state(session, req.user_id)
        if state is None:  # 탈퇴 — 처리 의미가 사라졌다(경보 대상 아님)
            raise JobCancelled("user_deleted")
        nickname, language = state

        # 세대부터 본다. 잡을 만든 뒤 forget이 실행됐으면 그 대화는 다시 요약하면 안 된다.
        # forget이 checkpoint를 전량 지운 직후라 previous=None이 되어, 체인 검사만으로는
        # "첫 요약"으로 보이고 그냥 통과한다 — 잊어달라고 한 대화가 요약으로 되살아난다.
        if not await _generation_matches(session, req):
            return JobResult(result_code=RESULT_STALE_GENERATION)

        applicable, previous = await _still_applicable(session, req)
        if not applicable:
            return JobResult(
                result_code=(
                    RESULT_ALREADY_CHECKPOINTED
                    if previous is not None
                    and previous.through_message_id >= req.through_message_id
                    else RESULT_SOURCE_CHANGED
                )
            )

        messages = await _load_sources(session, req, previous)
        if not messages:
            return JobResult(result_code=RESULT_SOURCE_CHANGED)
        if checkpoint.source_hash(previous=previous, messages=messages) != req.source_hash:
            # 같은 id 목록이라도 본문이 바뀌었다 = 이 요약의 전제가 깨졌다.
            return JobResult(result_code=RESULT_SOURCE_CHANGED)

        # 잊어줘가 닫은 메시지가 섞였으면 요약하지 않는다. forget은 **앵커를 전진시키지 않으므로**
        # 잊기 이후 만들어지는 일반 요약도 잊기 이전 메시지를 그대로 담는다 — 재검증만 막아서는
        # 구멍이 남는다(세대 검사도 못 잡는다. 이 잡은 잊기 *이후*에 만들어져 세대가 최신이다).
        if await checkpoint_repo.has_closed_messages(
            session, req.user_id, [m.id for m in messages]
        ):
            _log.info("요약 생략(잊어줘로 닫힌 메시지 포함) — user=%s", req.user_id)
            return JobResult(result_code=RESULT_SOURCE_CLOSED)

        # 몇 번째 checkpoint인가 — 매 N번째는 이전 요약 대신 원본으로 다시 요약해 누적 왜곡을 잰다.
        #
        # 재검증이 읽는 범위는 **그 유저의 경계까지 전체 이력**(`(0, through]`)이다. 체인이 실제로
        # 덮은 구간은 첫 checkpoint의 하한부터인데 그 하한을 따로 보관하지 않으므로, 모자란 쪽이
        # 아니라 **넘치는 쪽**(superset)을 택했다 — 앵커 이전 이력이 더 들어올 뿐 체인이 갖고 있던
        # 내용을 잃지 않는다. 반대로 "이번 구간 원본만"으로 좁히면 매 N번째마다 그 앞 이야기가
        # 통째로 사라진다(캐피가 10번에 한 번씩 기억을 잃는다).
        index = await checkpoint_repo.count(session, req.user_id) + 1
        every = max(int(settings.context_checkpoint_reverify_every), 1)
        # 재검증은 **검증할 체인이 있을 때만** 의미가 있다. previous가 없는 첫 요약에서 켜면
        # 원본이 비어 빈 입력으로 요약을 시도하고 잡이 재시도 끝에 dead가 된다
        # (기본값 10에선 index%10==0과 previous=None이 양립 불가라 안 드러나지만,
        #  reverify_every=1로 두는 순간 첫 요약부터 막힌다).
        reverify = index % every == 0 and previous is not None
        # 잊어줘가 닫은 구간이 있으면 재검증하지 않는다. 재검증은 `(0, through]` 원본 **전체**를
        # 다시 읽으므로 닫힌 구간을 필연적으로 포함하고, 원본 messages에는 잊은 내용이 그대로
        # 남아 있어 새 요약으로 재유입된다. 부분 필터링 대신 통째로 건너뛰고 체인 요약으로 간다 —
        # 재검증은 누적 왜곡을 재는 품질 장치지 정확성 장치가 아니라, 없어도 요약은 성립한다.
        if reverify and await checkpoint_repo.has_forget_closures(session, req.user_id):
            _log.info("요약 재검증 생략(잊어줘로 닫힌 구간 존재) — 체인 요약으로 진행")
            reverify = False
        originals: list[checkpoint.SourceMessage] = []
        if reverify:
            cap = int(settings.context_checkpoint_reverify_max_messages)
            originals = await checkpoint_repo.load_range(
                session, req.user_id, after_id=None, through_id=req.through_message_id,
                max_rows=cap + 1,
            )
            if len(originals) > cap:
                # 상한을 넘으면 **앞부분이** 잘린다(ORDER BY id ASC LIMIT) — 부분 이력을 전체인 양
                # 요약하지 않고 체인으로 간다.
                _log.info(
                    "요약 재검증 생략(원본 %d건 > 상한 %d) — 체인 요약으로 진행", len(originals), cap
                )
                reverify, originals = False, []

    # ── 여기서부터 DB 커넥션 0 (LLM 구간) ──
    inputs = originals if reverify else messages
    previous_summary = None if reverify or previous is None else previous.summary
    try:
        summary, call = await checkpoint.summarize(
            messages=inputs,
            previous_summary=previous_summary,
            language=language,
            nickname=nickname,
        )
    except checkpoint.NameLeakError as e:
        # 마스킹 후에도 실명이 남았다 — 저장하지 않고 재시도(다음 샘플은 통과할 수 있다).
        _log.warning("요약 실명 회귀 검사 실패(재시도) — job_id=%s: %s", job.id, e)
        raise JobRetry("summary_name_leak") from e
    except checkpoint.CheckpointError as e:  # 빈 요약 등 — 다음 샘플에 기대고 재시도
        raise JobRetry("summary_invalid") from e
    except Exception as e:  # noqa: BLE001  # provider 타임아웃·429·일시 장애 — backoff 재시도
        raise JobRetry("summary_llm_failed") from e

    async def _apply(session: AsyncSession) -> None:
        # 🔴 여기서 하는 도메인 쓰기는 checkpoint 1행뿐이다. 요약에서 기억 추출 잡을 만들지 않는다.
        # 여기 검사들은 **이른 종료용**이다. 잊어줘와의 경합에 대한 최종 방어는
        # `checkpoint_repo.insert`가 같은 문장 안에서 하는 세대 검사다 — 읽고 나서 쓰는
        # 사이를 잠금으로 막으려 하면 챗과 락 순서가 반대라 교착이 나고, 그걸 NOWAIT로
        # 피하면 재시도 횟수를 갉아먹어 요약이 영구 유실된다.
        ok, _ = await _still_applicable(session, req)
        if ok and await checkpoint_repo.has_closed_messages(
            session, req.user_id, list(req.source_message_ids)
        ):
            ok = False  # 잠금 획득 사이에 closure가 생겼다
        if not ok:
            _log.warning(
                "요약 확정 직전 상태 변화로 저장 생략 — user=%s through=%s", req.user_id,
                req.through_message_id,
            )
            return
        await checkpoint_repo.insert(
            session,
            user_id=req.user_id,
            through_message_id=req.through_message_id,
            summary=summary,
            source_hash=req.source_hash,
            expected_generation=req.memory_generation,
            version=req.version,
        )

    return JobResult(
        result_code=RESULT_OK,
        result_detail={
            "messages": len(inputs),
            "chars": len(summary),
            "reverified": reverify,
            "billable": call.billable,
        },
        apply_domain=_apply,
    )


# import 시 1회 등록(모듈 캐시 = 중복 등록 없음). consumer가 이 모듈을 지역 import 하는 이유는
# 순환 import 회피 — 여기서 consumer의 JobResult/JobRetry 계약을 쓰기 때문이다.
consumer.register(checkpoint.JOB_CONVERSATION_CHECKPOINT, handle_checkpoint)
