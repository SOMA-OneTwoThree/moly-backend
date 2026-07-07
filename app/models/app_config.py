"""app_config — 서버 설정 key-value (ERD §6.2). TBD 수치의 착지점.

DDL은 팀원 소유. 이 모델은 기존 테이블에 매핑되는 읽기용.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
