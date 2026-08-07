"""idempotency_keys — 재시도에 최초 성공 응답을 재사용하는 유저 범위 저장소.

채팅은 기존 raw key를 유지하고, 상점 구매는 endpoint prefix로 응답 형식을 격리한다.
raw key를 쓰는 라우트는 예약 prefix 키를 거부해 네임스페이스 위장을 차단해야 한다.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKeyConstraint, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


SHOP_PURCHASE_KEY_PREFIX = "shop-purchase:"

RESERVED_KEY_PREFIXES: tuple[str, ...] = (SHOP_PURCHASE_KEY_PREFIX,)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "reply_message_id"],
            ["messages.user_id", "messages.id"],
            ondelete="CASCADE",
        ),
    )

    # 복합 PK (user_id, key) — 유저 스코프. 다른 유저가 같은 키 써도 격리(응답 유출 방지).
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    # request_hash가 같은 재시도만 최초 결과를 hydrate한다. NULL은 migration 이전 legacy 행이다.
    request_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    response_schema_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    # legacy snapshot은 TTL 동안만 유지한다. 정본은 reply_message_id + chat_response_references다.
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reply_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    terminal_status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'succeeded'")
    )
    response_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dedupe_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
