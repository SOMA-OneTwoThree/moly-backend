"""relationship_profiles / relationship_profile_sources.

관계 프로필(W9) — 정규화 기억(W8)에서 파생한 **칸 고정** 프롬프트 투영. 렌더러는
`app/services/relationship_profile.py`, SQL·publish 트랜잭션은 `relationship_profile_repo.py`.

읽을 때 반드시 같이 봐야 하는 것:
- **locale당 published는 정확히 1개**다(부분 유니크 인덱스 `relationship_profiles_one_published_idx`).
  애플리케이션 검사만으로는 동시 publish를 못 막는다.
- `invalidated`/`superseded`는 **terminal**이다. published로 되돌리는 UPDATE 경로가 없어야 한다.
- `relationship_profile_sources`의 FK는 **복합키**(user_id 동반)라 타 유저 fact/insight를 근거로
  달 수 없다. 근거 FK는 ON DELETE RESTRICT — 근거 없는 프로필만 남는 상태를 스키마가 막는다.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)


class RelationshipProfile(Base):
    """locale별 프로필 문서 1버전. 상태 = draft | published | invalidated | superseded."""

    __tablename__ = "relationship_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # render_hash가 바뀔 때만 올라간다(같은 내용 재발행 = 안정 프리픽스 캐시 파괴).
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    locale: Mapped[str] = mapped_column(String, nullable=False)  # i18n 버킷(ko|en|ja)
    # draft를 만든 시점의 chat_contexts 값. publish가 잠근 현재값과 다르면 결과를 폐기한다.
    memory_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    relationship_profile_input_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 칸별 항목 + item_key + source_refs. edge 행과 type/id/item_key까지 양방향으로 같아야 한다.
    document_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 프롬프트에 실리는 문자열. 실명 대신 {유저이름} placeholder만 들어간다(저장 직전 마스킹 강제).
    rendered_text: Mapped[str] = mapped_column(String, nullable=False)
    render_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    published_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)


class RelationshipProfileSource(Base):
    """프로필 항목 ← fact/insight 간선. fact_id·insight_id 중 정확히 하나만 채워진다."""

    __tablename__ = "relationship_profile_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    relationship_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    item_key: Mapped[str] = mapped_column(String, nullable=False)
    fact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    insight_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
