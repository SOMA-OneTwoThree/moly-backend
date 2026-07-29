"""revenuecat_events — RC 웹훅 내구 inbox(SOMA-372).

엔드포인트가 event.id를 PK로 raw를 커밋(중복 웹훅 멱등) → process_event가 소비. DB 오류
삼킴으로 인한 구독·결제·건초 영구 유실을 막는 durable 경계. 미결 pending·미해결 failed는
워커가 매 틱 관측(은폐 없음).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RevenuecatEvent(Base):
    __tablename__ = "revenuecat_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)  # RC event.id(전역 유일)
    payload: Mapped[dict] = mapped_column(JSONB)
    # pending(미결·재시도 대상) | processed(처리 완료·no-op 포함) | failed(영구 오류·수동 처리)
    status: Mapped[str] = mapped_column(String, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
    # 다음 재시도 예약 시각 — 드레인 후보는 next_attempt_at <= now()만 선택하고 이 시각 순으로 정렬.
    # dependency_missing·예외 재시도로 재-pending 될 때 backoff만큼 뒤로 밀려 집합 내부 rotation 보장.
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 실패 사유 또는 의도적 no-op(상태 단조 skip·PRODUCT_CHANGE)의 durable reason.
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
