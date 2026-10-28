"""탐색 트레이닝 — 계정별 진행, 통과한 답, 보상 지급, 도전 기회, 채점 이의."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


def _owner() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )


class TrainingProgress(Base):
    __tablename__ = "training_progress"

    user_id: Mapped[uuid.UUID] = _owner()
    training_id: Mapped[str] = mapped_column(String, primary_key=True)
    # 다음에 풀 단계. 다 끝내면 NULL이고 completed_at이 채워진다.
    next_stage: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    started_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    # 마지막 제출의 요청 id와 판정. 응답을 잃은 재전송에 같은 판정을 돌려준다.
    last_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # none_as_null: None 대입이 jsonb 'null'로 저장되면 짝 CHECK·object CHECK 위반.
    last_result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class TrainingAnswer(Base):
    """통과한 답만 둔다. 고른 보기 id나 쓴 글 중 하나다."""

    __tablename__ = "training_answers"

    user_id: Mapped[uuid.UUID] = _owner()
    training_id: Mapped[str] = mapped_column(String, primary_key=True)
    stage: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    option_id: Mapped[str | None] = mapped_column(String, nullable=True)
    answer_text: Mapped[str | None] = mapped_column(String, nullable=True)
    passed_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class TrainingReward(Base):
    """돌아보기 지점 보상 지급 기록. 다시 풀어도 지우지 않아 계정당 한 번만 준다."""

    __tablename__ = "training_rewards"

    user_id: Mapped[uuid.UUID] = _owner()
    training_id: Mapped[str] = mapped_column(String, primary_key=True)
    stage: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class TrainingTries(Base):
    """계정 하나에 하나. 행이 없으면 5개로 본다."""

    __tablename__ = "training_tries"

    user_id: Mapped[uuid.UUID] = _owner()
    tries_left: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # 지금 충전 주기의 시작 시각. 5개가 다 차 있으면 NULL.
    refill_from: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class TrainingClaim(Base):
    __tablename__ = "training_claims"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    training_id: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    answer_text: Mapped[str] = mapped_column(String, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    hint: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class TrainingSubmission(Base):
    """요청별 판정과 채점 선점. 실패한 답 원문은 보관하지 않는다."""

    __tablename__ = "training_submissions"

    user_id: Mapped[uuid.UUID] = _owner()
    training_id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    stage: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    lease_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
