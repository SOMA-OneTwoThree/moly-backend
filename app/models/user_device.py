"""user_devices — FCM 푸시 토큰(iOS는 APNs 릴레이·Android 네이티브, ERD §6.4). push_token UNIQUE로 중복 제거."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class UserDevice(Base):
    __tablename__ = "user_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    platform: Mapped[str] = mapped_column(String)  # ios | android
    push_token: Mapped[str] = mapped_column(String, unique=True)
    last_active_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(_TZ, server_default=text("now()"), nullable=True)
    # FCM이 토큰 무효(UNREGISTERED 등)를 확정한 시각. NULL=유효. last_active_at이 더 최신이면
    # (앱이 같은 토큰을 다시 등록) 유효로 본다 — notify._tokens 참조. moly-auth는 이 컬럼을 모른다.
    invalidated_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
