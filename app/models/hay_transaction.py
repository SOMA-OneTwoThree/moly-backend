"""hay_transactions — 건초 원장(ERD §4.1). 모든 획득/소비의 단일 진실. balance는 profiles 캐시.

구매 관련 원장(iap_purchase·shop_purchase)은 order_id로 주문과 연결 — CS 추적 자동화.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class HayTransaction(Base):
    __tablename__ = "hay_transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    # attendance ad_reward routine_reward iap_purchase subscription_grant shop_purchase refund_revoke admin_adjustment
    type: Mapped[str] = mapped_column(String)
    amount: Mapped[int] = mapped_column(Integer)  # +획득 / −소비
    balance_after: Mapped[int] = mapped_column(Integer)
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
