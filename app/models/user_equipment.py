"""user_equipment — 슬롯당 1행, 행 없으면 기본 상태(ERD §4.9). enum은 String 매핑."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class UserEquipment(Base):
    __tablename__ = "user_equipment"

    # 유니크 (user_id, slot) — 슬롯당 1행. 복합 PK로 표현.
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slot: Mapped[str] = mapped_column(String, primary_key=True)  # background | head | neck | body
    shop_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    equipped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
