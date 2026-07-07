"""user_notification_settings — 알림 2종 on/off(ERD §6.3). 행 없으면 enabled=true(기본 on)."""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class UserNotificationSettings(Base):
    __tablename__ = "user_notification_settings"

    # 유니크 (user_id, type) — 복합 PK로 표현.
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    type: Mapped[str] = mapped_column(String, primary_key=True)  # morning_diary | evening_chat
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
