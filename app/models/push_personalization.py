"""push_personalizations — 저녁 푸시 개인화 문구(유저당 1행, 사이클마다 덮어씀).

body는 naming placeholder 상태로만 저장(실명 저장 금지 — 발송 직전 render).
대화 파생 PII라 RLS + anon/authenticated REVOKE(등급 2, memory_*와 동일).
재사용 한도(D+3)의 정본은 anchor_date 날짜 산술 — sent_count는 통계 전용(판정 사용 금지).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import Date, DateTime, Integer, String, Time, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PushPersonalization(Base):
    __tablename__ = "push_personalizations"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    anchor_date: Mapped[date] = mapped_column(Date)
    send_slot: Mapped[time] = mapped_column(Time)  # [08:00, 20:00] 15분 격자, 20:00=야간 코호트
    body: Mapped[str] = mapped_column(String)  # placeholder 상태({유저이름} 토큰)
    language: Mapped[str] = mapped_column(String)
    source_kind: Mapped[str] = mapped_column(String)  # v2부터 항상 transcript(구 행만 diary)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    sent_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_sent_on: Mapped[date | None] = mapped_column(Date, nullable=True)
