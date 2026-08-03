"""memory_source_turns / memory_source_turn_messages / memory_source_closures.

기억 추출의 **소스 좌표계**(W8). watermark는 대화 turn당 정확히 하나이고, 한 message는 정확히 한
watermark에만 속한다 → `memory_evidence.source_id`의 watermark를 turn_messages로 되찾는다.

closure는 forget이 닫은 구간이다. 추출 배치의 중간 watermark가 **하나라도** 겹치면 부분 publish
없이 전체를 `source_range_closed`로 끝내고, 열린 source만 새 generation job으로 다시 묶는다.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class MemorySourceTurn(Base):
    """turn 대표행. `representative_message_id` = 그 turn을 시작한 **inbound user message**.

    이 메시지가 없는 turn(선발화만 있는 turn 등)은 v1에서 추출 소스로 enqueue하지 않는다.
    """

    __tablename__ = "memory_source_turns"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_watermark: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    representative_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    committed_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)


class MemorySourceTurnMessage(Base):
    """그 turn에서 evidence로 허용할 user/assistant 메시지 전부(대표 포함). UNIQUE(user_id, message_id)."""

    __tablename__ = "memory_source_turn_messages"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_watermark: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class MemorySourceClosure(Base):
    """forget이 닫은 소스 구간(영속). 계정 삭제 전에는 삭제·축소하지 않는다."""

    __tablename__ = "memory_source_closures"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String, nullable=False)  # v1 = conversation_turn만
    from_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    through_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    forget_operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
