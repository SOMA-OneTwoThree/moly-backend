"""profiles — auth.users와 1:1. 가입 트리거로 자동 생성(ERD §3.2). DDL은 팀원 소유."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    nickname: Mapped[str | None] = mapped_column(String, nullable=True)
    language: Mapped[str] = mapped_column(String, server_default=text("'ko'"))
    timezone: Mapped[str] = mapped_column(String, server_default=text("'Asia/Seoul'"))
    hay_balance: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    trial_ends_at: Mapped[datetime | None] = mapped_column(nullable=True)
    review_prompted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(server_default=text("now()"), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(server_default=text("now()"), nullable=True)
