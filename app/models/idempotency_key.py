"""idempotency_keys — POST /chat/messages 멱등(API_SPEC §1).

⚠️ ERD 밖(백엔드 신규 도입). 재시도 시 저장된 응답을 그대로 반환해 이중 전송·이중 차감 방지.
**팀원 DB 스키마에 추가 필요** — 테이블: key(pk)·user_id·response(jsonb)·created_at.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    # 복합 PK (user_id, key) — 유저 스코프. 다른 유저가 같은 키 써도 격리(응답 유출 방지).
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    response: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
