"""reward_ad_sessions — 리워드 광고 세션(SSV 자동 지급 소스).

플로우: POST /reward-ad-sessions(한도 확인)로 세션 발급 → 클라가 세션 id를 SSV
custom_data에 실어 광고 시청 → AdMob SSV 콜백이 세션 조회 후 +10 지급.
멱등 = **세션당 1회 지급**(`granted` 행잠금) + `ssv_transaction_id` UNIQUE(재전송 방어).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RewardAdSession(Base):
    __tablename__ = "reward_ad_sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    activity_date: Mapped[date] = mapped_column(Date)
    ssv_transaction_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    granted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
