"""탐색 트레이닝 API — 목록, 상세, 시작, 답 제출, 다시 풀기, 채점 이의."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.fortune import FortuneLocaleHeader
from app.schemas.trainings import (
    TrainingAnswerRequest,
    TrainingAnswerResult,
    TrainingClaimRequest,
    TrainingDetail,
    TrainingListResponse,
    TrainingProgress,
)
from app.services import trainings

router = APIRouter(tags=["training"])


def _locale(
    locale: FortuneLocaleHeader | None = Header(
        default=None,
        alias="X-App-Locale",
        description=(
            "트레이닝 문구 언어. ko, en, ja 또는 해당 언어의 지역 태그(예: ko-KR)를 받는다. "
            "없으면 계정 언어를 쓴다."
        ),
    ),
) -> str | None:
    return locale


@router.get("/trainings", response_model=TrainingListResponse)
async def list_trainings(
    locale: str | None = Depends(_locale),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await trainings.list_trainings(session, user_id, locale=locale)


@router.get("/trainings/{training_id}", response_model=TrainingDetail)
async def get_training(
    training_id: str,
    locale: str | None = Depends(_locale),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await trainings.get_training(session, user_id, training_id, locale=locale)


@router.post("/trainings/{training_id}/start", response_model=TrainingProgress)
async def start_training(
    training_id: str,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await trainings.start(session, user_id, training_id)


@router.post("/trainings/{training_id}/stages/{stage}/answers", response_model=TrainingAnswerResult)
async def submit_training_answer(
    training_id: str,
    stage: int,
    body: TrainingAnswerRequest,
    locale: str | None = Depends(_locale),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await trainings.submit(session, user_id, training_id, stage, body, locale=locale)


@router.post("/trainings/{training_id}/restart", response_model=TrainingDetail)
async def restart_training(
    training_id: str,
    locale: str | None = Depends(_locale),
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await trainings.restart(session, user_id, training_id, locale=locale)


@router.post("/trainings/{training_id}/stages/{stage}/claims", status_code=status.HTTP_204_NO_CONTENT)
async def claim_training_grade(
    training_id: str,
    stage: int,
    body: TrainingClaimRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await trainings.claim(session, user_id, training_id, stage, body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
