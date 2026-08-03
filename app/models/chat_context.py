"""chat_contexts — 유저별 대화 컨텍스트 상태(앵커 + 기억 스냅샷).

앵커: 프롬프트에 넣을 세그먼트 시작 메시지 id(append-only, 리셋 시에만 전진).
memory_text: mem0 렌더 스냅샷(핫패스에서 mem0 제거). memory_refreshed_at로 갱신 판정.
민감 데이터(기억 평문 사본) → RLS deny-default + profiles ON DELETE CASCADE(마이그레이션).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ChatContext(Base):
    __tablename__ = "chat_contexts"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    anchor_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    memory_text: Mapped[str | None] = mapped_column(String, nullable=True)
    memory_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 정규화 기억(W8) — 유저별 모드/세대/소스 워터마크/프로필 입력 리비전.
    # 'legacy'(mem0 자유문) | 'normalized'(memory_facts). W8은 스키마·저장소만 놓고 유저를 옮기지
    # 않는다(전환=W10 cutover). memory_text/memory_refreshed_at은 legacy 전용이다.
    memory_mode: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'legacy'")
    )
    # forget/cutover마다 +1 → 늦게 돌아온 잡의 stale 결과를 버리는 기준.
    memory_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # 대화 turn 커밋마다 +1로 배정(memory_source_turns의 watermark).
    memory_source_watermark: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # fact/insight의 **실제** 내용·source·상태가 바뀐 트랜잭션에서만 정확히 +1.
    # no-op/retry/임베딩 재색인은 증가시키지 않는다(프로필 재생성 폭주 방지).
    relationship_profile_input_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
