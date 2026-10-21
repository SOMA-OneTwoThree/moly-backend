"""탐색 트레이닝 — 목록, 상세, 시작, 답 제출, 다시 풀기, 채점 이의.

답 제출은 외부 모델을 부르는 동안 DB 트랜잭션을 열어 두지 않는다(README). 주관식은 트랜잭션
1에서 읽기만 하고 끝낸 뒤 채점하고, 트랜잭션 2에서 사용자 잠금 아래 삭제 장벽·요청 id·단계·
기회를 다시 확인해 쓴다. 객관식은 바깥 호출이 없어 잠금 뒤 한 트랜잭션이다.

같은 요청 id로 다시 오면 저장해 둔 판정을 돌려주고 기회를 다시 쓰지 않는다. 통과하지 못한 답의
글은 저장하지 않는다(판정·힌트만 남긴다).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.advisory_lock import advisory_xact_lock
from app.models.profile import Profile
from app.models.training import (
    TrainingAnswer,
    TrainingClaim,
    TrainingProgress,
    TrainingReward,
    TrainingTries,
)
from app.schemas.trainings import TrainingAnswerRequest, TrainingClaimRequest
from app.services import hay_ledger, i18n, privacy, training_catalog, training_grading, training_tries
from app.services.training_catalog import TrainingDefinition, TrainingStage
from app.services.training_grading import Verdict
from app.services.training_tries import Tries

REWARD_TYPE = "training_reward"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _not_found() -> errors.AppError:
    return errors.AppError("TRAINING_NOT_FOUND", 404, "트레이닝을 찾을 수 없어요.")


def _not_current() -> errors.AppError:
    return errors.AppError("TRAINING_STAGE_NOT_CURRENT", 409, "지금 풀 차례인 단계가 아니에요.")


def _no_tries(tries: Tries) -> errors.AppError:
    return errors.AppError(
        "TRAINING_NO_TRIES", 409, "도전 기회를 모두 썼어요.", {"tries": _tries_wire(tries)}
    )


def _training(training_id: str) -> TrainingDefinition:
    training = training_catalog.current().get(training_id)
    if training is None:
        raise _not_found()
    return training


async def _language(session: AsyncSession, user_id: str, locale: str | None) -> str:
    """헤더(이미 ko·en·ja로 정규화됨)를 쓰고, 없으면 계정 언어. 지원 밖은 en."""
    if locale is not None:
        return locale
    return i18n.resolve(await _account_language(session, uuid.UUID(user_id)))


# --- DB 읽기·쓰기. 단위 테스트는 이 함수들을 바꿔 끼운다. ---
# 트랜잭션 2는 트랜잭션 1에서 읽은 객체를 다시 쓰지 않도록 populate_existing으로 새로 읽는다.


async def _account_language(session: AsyncSession, uid: uuid.UUID) -> str | None:
    # 열 하나만 읽는다. Profile 객체를 세션에 올려 두면 보상 때 hay_ledger.apply가 잠근 행 대신
    # 그 객체의 옛 잔액을 써서, AI 채점 중에 바뀐 잔액을 덮어쓴다.
    return await session.scalar(select(Profile.language).where(Profile.id == uid))


async def _load_progress(
    session: AsyncSession, uid: uuid.UUID, training_id: str
) -> TrainingProgress | None:
    return await session.get(TrainingProgress, (uid, training_id), populate_existing=True)


async def _load_all_progress(session: AsyncSession, uid: uuid.UUID) -> dict[str, TrainingProgress]:
    rows = await session.execute(select(TrainingProgress).where(TrainingProgress.user_id == uid))
    return {row.training_id: row for row in rows.scalars()}


async def _load_answers(
    session: AsyncSession, uid: uuid.UUID, training_id: str
) -> list[TrainingAnswer]:
    rows = await session.execute(
        select(TrainingAnswer)
        .where(TrainingAnswer.user_id == uid, TrainingAnswer.training_id == training_id)
        .order_by(TrainingAnswer.stage)
    )
    return list(rows.scalars())


async def _load_answer(
    session: AsyncSession, uid: uuid.UUID, training_id: str, stage: int
) -> TrainingAnswer | None:
    return await session.get(TrainingAnswer, (uid, training_id, stage))


async def _load_tries(session: AsyncSession, uid: uuid.UUID) -> TrainingTries | None:
    return await session.get(TrainingTries, uid, populate_existing=True)


async def _reward_granted(
    session: AsyncSession, uid: uuid.UUID, training_id: str, stage: int
) -> bool:
    return await session.get(TrainingReward, (uid, training_id, stage)) is not None


async def _delete_answers(session: AsyncSession, uid: uuid.UUID, training_id: str) -> None:
    await session.execute(
        delete(TrainingAnswer).where(
            TrainingAnswer.user_id == uid, TrainingAnswer.training_id == training_id
        )
    )


# --- 응답 모양 ---


def _settled(row: TrainingTries | None, now: datetime) -> Tries:
    if row is None:
        return training_tries.FULL
    return training_tries.settle(row.tries_left, row.refill_from, now)


def _tries_wire(tries: Tries) -> dict[str, Any]:
    return {
        "left": tries.left,
        "max": training_tries.MAX_TRIES,
        "next_refill_at": tries.next_refill_at,
        "full_at": tries.full_at,
    }


def _progress_wire(row: TrainingProgress | None, training: TrainingDefinition) -> dict[str, Any]:
    if row is None:
        return {"started": False, "next_stage": 1, "passed_count": 0, "completed": False}
    completed = row.next_stage is None
    return {
        "started": True,
        "next_stage": row.next_stage,
        "passed_count": len(training.stages) if completed else row.next_stage - 1,
        "completed": completed,
    }


def _answer_wire(row: TrainingAnswer) -> dict[str, Any]:
    return {
        "stage": row.stage,
        "option_id": row.option_id,
        "text": row.answer_text,
        "passed_at": row.passed_at,
    }


def _new_progress(uid: uuid.UUID, training_id: str, now: datetime) -> TrainingProgress:
    return TrainingProgress(
        user_id=uid, training_id=training_id, next_stage=1, started_at=now,
        completed_at=None, last_request_id=None, last_result=None, updated_at=now,
    )


async def _detail(
    session: AsyncSession, uid: uuid.UUID, training: TrainingDefinition, language: str, now: datetime
) -> dict[str, Any]:
    progress = await _load_progress(session, uid, training.id)
    answers = await _load_answers(session, uid, training.id)
    tries = _settled(await _load_tries(session, uid), now)
    return {
        "id": training.id,
        "title": training.title.pick(language),
        "summary": training.summary.pick(language),
        "icon_key": training.icon_key,
        "stages": [training_catalog.public_stage(stage, language) for stage in training.stages],
        "progress": _progress_wire(progress, training),
        "answers": [_answer_wire(answer) for answer in answers],
        "tries": _tries_wire(tries),
    }


# --- 공개 함수 ---


async def list_trainings(session: AsyncSession, user_id: str, *, locale: str | None) -> dict[str, Any]:
    catalog = training_catalog.current()
    if not catalog.trainings:
        return {"trainings": []}
    uid = uuid.UUID(user_id)
    await privacy.ensure_subject_active(session, uid)
    language = await _language(session, user_id, locale)
    progress = await _load_all_progress(session, uid)
    cards = []
    for training in catalog.trainings:
        wire = _progress_wire(progress.get(training.id), training)
        cards.append({
            "id": training.id,
            "title": training.title.pick(language),
            "icon_key": training.icon_key,
            "stage_count": len(training.stages),
            "passed_count": wire["passed_count"],
            "completed": wire["completed"],
        })
    return {"trainings": cards}


async def get_training(
    session: AsyncSession, user_id: str, training_id: str, *, locale: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    training = _training(training_id)
    uid = uuid.UUID(user_id)
    await privacy.ensure_subject_active(session, uid)
    language = await _language(session, user_id, locale)
    return await _detail(session, uid, training, language, now or _now())


async def start(
    session: AsyncSession, user_id: str, training_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """처음 안내를 본 뒤 부른다. 이미 시작했으면 그대로 둔다."""
    training = _training(training_id)
    uid = uuid.UUID(user_id)
    await advisory_xact_lock(session, uid)
    await privacy.ensure_subject_active(session, uid)
    progress = await _load_progress(session, uid, training.id)
    if progress is None:
        progress = _new_progress(uid, training.id, now or _now())
        session.add(progress)
    await session.commit()
    return _progress_wire(progress, training)


async def restart(
    session: AsyncSession, user_id: str, training_id: str, *, locale: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """진행과 답을 지우고 1단계부터. 보상 기록과 도전 기회는 그대로 둔다(요구사항 10.2)."""
    training = _training(training_id)
    uid = uuid.UUID(user_id)
    now = now or _now()
    language = await _language(session, user_id, locale)
    await advisory_xact_lock(session, uid)
    await privacy.ensure_subject_active(session, uid)
    await _delete_answers(session, uid, training.id)
    progress = await _load_progress(session, uid, training.id)
    if progress is None:
        session.add(_new_progress(uid, training.id, now))
    else:
        progress.next_stage = 1
        progress.completed_at = None
        progress.last_request_id = None
        progress.last_result = None
        progress.updated_at = now
    detail = await _detail(session, uid, training, language, now)
    await session.commit()
    return detail


async def submit(
    session: AsyncSession, user_id: str, training_id: str, stage_number: int,
    body: TrainingAnswerRequest, *, locale: str | None, now: datetime | None = None,
) -> dict[str, Any]:
    training = _training(training_id)
    stage = training.stage(stage_number)
    if stage is None:
        raise _not_found()
    if stage.answer_type == "pick":
        if body.option_id is None or stage.option(body.option_id) is None:
            raise errors.validation("보기를 다시 골라 주세요.")
    elif body.text is None:
        raise errors.validation("답을 적어 주세요.")
    uid = uuid.UUID(user_id)
    now = now or _now()
    language = await _language(session, user_id, locale)
    if stage.answer_type == "pick":
        return await _submit_pick(session, uid, training, stage, body, language, now)
    return await _submit_write(session, uid, training, stage, body, language, now)


async def claim(
    session: AsyncSession, user_id: str, training_id: str, stage_number: int,
    body: TrainingClaimRequest,
) -> None:
    """채점 이의를 남긴다. 판정은 바꾸지 않는다(요구사항 20.1)."""
    stage = _training(training_id).stage(stage_number)
    if stage is None:
        raise _not_found()
    if stage.answer_type != "write":
        raise errors.validation("주관식 단계에만 이의를 남길 수 있어요.")
    uid = uuid.UUID(user_id)
    await privacy.ensure_subject_active(session, uid)
    session.add(TrainingClaim(
        user_id=uid, training_id=training_id, stage=stage_number, reason=body.reason,
        answer_text=body.answer_text, verdict=body.verdict, hint=body.hint,
    ))
    await session.commit()


# --- 제출 ---


def _require_current(progress: TrainingProgress | None, stage: TrainingStage) -> None:
    if stage.number != (1 if progress is None else progress.next_stage):
        raise _not_current()


async def _replay(
    session: AsyncSession, uid: uuid.UUID, training: TrainingDefinition, stage: TrainingStage,
    body: TrainingAnswerRequest, progress: TrainingProgress | None, now: datetime,
) -> dict[str, Any] | None:
    """같은 요청 id를 이미 처리했으면 그 판정에 지금 진행·기회를 붙여 돌려준다."""
    if progress is None or progress.last_request_id != body.client_request_id:
        return None
    saved = progress.last_result or {}
    if saved.get("stage") != stage.number:
        return None
    answer = None
    if saved["result"] == "passed":
        answer = await _load_answer(session, uid, training.id, stage.number)
    return {
        "result": saved["result"],
        "judge": saved["judge"],
        "hint": saved.get("hint"),
        "explanation": saved.get("explanation"),
        "reward": saved.get("reward"),
        "answer": _answer_wire(answer) if answer is not None else None,
        "progress": _progress_wire(progress, training),
        "tries": _tries_wire(_settled(await _load_tries(session, uid), now)),
    }


async def _submit_pick(
    session: AsyncSession, uid: uuid.UUID, training: TrainingDefinition, stage: TrainingStage,
    body: TrainingAnswerRequest, language: str, now: datetime,
) -> dict[str, Any]:
    await advisory_xact_lock(session, uid)
    await privacy.ensure_subject_active(session, uid)
    progress = await _load_progress(session, uid, training.id)
    replay = await _replay(session, uid, training, stage, body, progress, now)
    if replay is not None:
        await session.commit()
        return replay
    _require_current(progress, stage)
    tries_row = await _load_tries(session, uid)
    tries = _settled(tries_row, now)
    # 정답을 맞혀 보는 것도 막아야 하므로 판정보다 먼저 확인한다.
    if tries.left == 0:
        raise _no_tries(tries)
    verdict = training_grading.grade_pick(stage, body.option_id, language)
    result = await _record(session, uid, training, stage, body, progress, tries_row, tries, verdict, language, now)
    await session.commit()
    return result


async def _submit_write(
    session: AsyncSession, uid: uuid.UUID, training: TrainingDefinition, stage: TrainingStage,
    body: TrainingAnswerRequest, language: str, now: datetime,
) -> dict[str, Any]:
    # 트랜잭션 1: 읽기만 한다.
    await privacy.ensure_subject_active(session, uid)
    progress = await _load_progress(session, uid, training.id)
    replay = await _replay(session, uid, training, stage, body, progress, now)
    if replay is not None:
        await session.commit()
        return replay
    _require_current(progress, stage)
    tries = _settled(await _load_tries(session, uid), now)
    if tries.left == 0:
        raise _no_tries(tries)
    # 커밋하면 커넥션이 풀로 돌아가 채점하는 동안 DB를 쥐지 않는다.
    await session.commit()

    verdict = await training_grading.grade_write(stage, body.text, language, user_id=uid)

    # 트랜잭션 2: 그사이 바뀐 것을 잠금 아래 다시 확인하고 쓴다.
    await advisory_xact_lock(session, uid)
    await privacy.ensure_subject_active(session, uid)
    progress = await _load_progress(session, uid, training.id)
    replay = await _replay(session, uid, training, stage, body, progress, now)
    if replay is not None:
        await session.commit()
        return replay
    _require_current(progress, stage)
    tries_row = await _load_tries(session, uid)
    tries = _settled(tries_row, now)
    # 그사이 다른 제출이 마지막 기회를 썼으면 불통과는 받지 않는다. 통과는 기회를 쓰지 않아 받는다.
    if not verdict.passed and tries.left == 0:
        raise _no_tries(tries)
    result = await _record(session, uid, training, stage, body, progress, tries_row, tries, verdict, language, now)
    await session.commit()
    return result


async def _record(
    session: AsyncSession, uid: uuid.UUID, training: TrainingDefinition, stage: TrainingStage,
    body: TrainingAnswerRequest, progress: TrainingProgress | None, tries_row: TrainingTries | None,
    tries: Tries, verdict: Verdict, language: str, now: datetime,
) -> dict[str, Any]:
    if progress is None:
        progress = _new_progress(uid, training.id, now)
        session.add(progress)
    answer = None
    reward = None
    if verdict.passed:
        answer = TrainingAnswer(
            user_id=uid, training_id=training.id, stage=stage.number,
            option_id=body.option_id, answer_text=body.text, passed_at=now,
        )
        session.add(answer)
        last = stage.number == len(training.stages)
        progress.next_stage = None if last else stage.number + 1
        progress.completed_at = now if last else None
        if stage.review_point and not await _reward_granted(session, uid, training.id, stage.number):
            tx = await hay_ledger.apply(session, uid, REWARD_TYPE, stage.reward_hay)
            session.add(TrainingReward(
                user_id=uid, training_id=training.id, stage=stage.number,
                amount=stage.reward_hay, granted_at=now,
            ))
            reward = {"granted": stage.reward_hay, "balance_after": tx.balance_after}
    else:
        tries = training_tries.spend(tries, now)
        if tries_row is None:
            session.add(TrainingTries(
                user_id=uid, tries_left=tries.left, refill_from=tries.refill_from, updated_at=now,
            ))
        else:
            tries_row.tries_left = tries.left
            tries_row.refill_from = tries.refill_from
            tries_row.updated_at = now
    saved = {
        "stage": stage.number,
        "result": "passed" if verdict.passed else "retry",
        "judge": verdict.judge,
        "hint": verdict.hint,
        "explanation": stage.explanation.pick(language) if verdict.passed and stage.explanation else None,
        "reward": reward,
    }
    progress.last_request_id = body.client_request_id
    progress.last_result = saved
    progress.updated_at = now
    await session.flush()
    return {
        "result": saved["result"],
        "judge": saved["judge"],
        "hint": saved["hint"],
        "explanation": saved["explanation"],
        "reward": reward,
        "answer": _answer_wire(answer) if answer is not None else None,
        "progress": _progress_wire(progress, training),
        "tries": _tries_wire(tries),
    }
