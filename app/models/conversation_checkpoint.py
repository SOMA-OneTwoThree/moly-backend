"""conversation_checkpoints — 대화 요약 checkpoint(W11).

대화가 길어지면 오래된 구간을 요약해 한 행으로 남기고, 다음 턴은 **가장 앞선 checkpoint 하나 +
그 이후 메시지**만 쓴다. `source_hash`는 이전 checkpoint와 이번 원본 메시지에서 결정적으로 나오는
입력 지문이라 같은 입력이 두 번 요약되지 않는다(UNIQUE + 잡 dedup key). 로직은
`app/services/checkpoint.py`(순수)와 `app/services/checkpoint_repo.py`(SQL).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ConversationCheckpoint(Base):
    __tablename__ = "conversation_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # 이 요약이 덮는 마지막 메시지(경계). ON DELETE RESTRICT — 사라지면 범위를 되짚을 수 없다.
    through_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)  # 실명 금지({유저이름} placeholder)
    version: Mapped[str] = mapped_column(String, nullable=False)  # 요약기 계약 버전
    source_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
