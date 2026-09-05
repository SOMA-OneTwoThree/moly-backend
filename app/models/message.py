"""messages — 단일 연속 스레드(ERD §5.2). kind='normal'만 토큰 집계."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Integer, SmallInteger, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Message(Base):
    __tablename__ = "messages"
    # 모든 파생/참조 테이블은 user_id를 FK에 함께 태워 tenant 경계를 DB에서도 강제한다.
    # sender까지 포함한 키는 memory_evidence가 user 발화만 근거로 삼도록 하는 FK 대상이다.
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="messages_user_id_id_uq"),
        UniqueConstraint("user_id", "id", "sender", name="messages_user_id_id_sender_uq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    sender: Mapped[str] = mapped_column(String)  # user | moly
    # fortune_*는 원문 대화에는 보이되 기억·일기·관계 같은 파생 파이프라인에서 제외한다.
    kind: Mapped[str] = mapped_column(String, server_default=text("'normal'"))
    turn_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    turn_position: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    content: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 캐시 텔레메트리(실원가·히트율 집계용) + 청구 스냅샷(가중치 변경 후 재감사).
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    billable_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activity_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
