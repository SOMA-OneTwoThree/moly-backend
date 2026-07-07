"""ad_rewards — 광고 SSV 확정 기록(멱등·수령). ERD 밖 신규(팀원 DB 추가 필요).

웹훅(SSV)이 확정 레코드 삽입(granted=false) → /ads/reward가 수령 처리(granted=true).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AdReward(Base):
    __tablename__ = "ad_rewards"

    ssv_transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    activity_date: Mapped[date] = mapped_column(Date)
    granted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
