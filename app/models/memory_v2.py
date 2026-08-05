"""기억 v2 — mem0 파이프라인·registry·대화 계약·관계 상태 모델.

기억 재설계 4단계(docs/capi-memory-ARCHITECTURE.md 15장 4번). legacy 정규화 기억 테이블과
**함께 존재**하며, shadow → cutover → DROP 순서로만 legacy를 제거한다.

source 좌표는 `(user_id, turn_seq)` 하나다(13.3절). 로직·SQL은 각 서비스 모듈이 소유하고
여기엔 스키마 거울만 둔다.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_TZ = DateTime(timezone=True)

# 사용자별 전환 모드 — v2 응답 사용 여부를 가른다.
MODE_LEGACY = "legacy"
MODE_SHADOW = "shadow"
MODE_V2 = "v2"

# bootstrap — historical backfill이 끝나기 전에 live turn을 먼저 처리하지 않게 한다.
BOOTSTRAP_LEGACY = "legacy"
BOOTSTRAP_COLLECTING = "collecting"
BOOTSTRAP_READY = "ready"


class MemoryPipelineState(Base):
    """사용자별 ingest/consolidation 커서와 stage lease.

    외부 호출 동안 advisory lock을 잡지 않기 위해 짧은 transaction CAS(`stage_token`/`revision`)로
    한 turn만 선점한다(9.2절). cursor는 숫자 +1이 아니라 source table의 다음 MIN(turn_seq)로 전진한다.
    """

    __tablename__ = "memory_pipeline_states"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_through_turn_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    ingest_through_turn_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    consolidated_through_turn_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    active_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    stage_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    privacy_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    repair_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    bootstrap_status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'legacy'")
    )
    historical_upper_turn_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mode: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'legacy'"))
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class Mem0IngestCandidate(Base):
    """provider 호출 **전에** 저장하는 durable plan.

    결정 UUID(`provider_memory_id`)를 미리 정해두기 때문에 provider 성공 직후 crash가 나도
    재시도가 같은 행으로 수렴한다(랜덤 중복 방지, 9.2절).
    """

    __tablename__ = "mem0_ingest_candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    turn_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[str] = mapped_column(String, nullable=False)
    extractor_version: Mapped[str] = mapped_column(String, nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String, nullable=False)
    provider_memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    candidate_text: Mapped[str] = mapped_column(String, nullable=False)
    temporal_proposal_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    event_started_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    event_ended_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    event_time_precision: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'planned'"))
    repair_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    scrubbed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class Mem0IngestCandidateSource(Base):
    """후보의 근거 span. `sender='user'`만 독립 근거다(assistant는 context_only)."""

    __tablename__ = "mem0_ingest_candidate_sources"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_sender: Mapped[str] = mapped_column(String, nullable=False)
    source_content_hash: Mapped[str] = mapped_column(String, nullable=False)
    evidence_start_utf8: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_end_utf8: Mapped[int] = mapped_column(Integer, primary_key=True)
    authority: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class Mem0MemoryRegistry(Base):
    """provider memory id의 수명. **본문·임베딩은 복제하지 않는다.**

    mem0가 candidate-add-only라 과거와 현재의 상반된 기억이 함께 남는다. 어느 쪽이 현재인지는
    여기서 판정하고(`semantic_status`), 검색은 active/ambiguous만 통과시킨다(9.4절).
    provider delete가 늦어도 이 필터가 과거 값을 프롬프트에서 막는다.
    """

    __tablename__ = "mem0_memory_registry"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    collection_version: Mapped[str] = mapped_column(String, nullable=False)
    provider_memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_turn_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    event_started_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    event_ended_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    event_time_precision: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    temporal_resolver_version: Mapped[str | None] = mapped_column(String, nullable=True)
    semantic_status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'pending'")
    )
    provider_delete_state: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'kept'")
    )
    provider_deleted_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    conflict_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    duplicate_of_registry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    superseded_by_registry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    classification_version: Mapped[str | None] = mapped_column(String, nullable=True)
    schema_version: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_confirmed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_source_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class Mem0MemorySource(Base):
    """source hydration·tombstone 제외·activity-date rerank·감사의 **DB 정본**.

    provider metadata는 복구 보조 사본일 뿐 source 정본이 아니다(9.4절).
    """

    __tablename__ = "mem0_memory_sources"

    registry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_turn_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_sender: Mapped[str] = mapped_column(String, nullable=False)
    evidence_start_utf8: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_end_utf8: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_content_hash: Mapped[str] = mapped_column(String, nullable=False)
    # 사용자가 그 말을 보낸 시각. 사건 시각(event_*)과 다르다.
    source_occurred_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    source_activity_date: Mapped[date] = mapped_column(Date, nullable=False)
    authority: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    extractor_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class UserInteractionContract(Base):
    """사용자별 대화 계약. 검색 성공 여부와 무관하게 **항상 주입**된다(6절).

    사용자·locale별 published는 정확히 1개이며 DB partial unique index가 강제한다.
    """

    __tablename__ = "user_interaction_contracts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    locale: Mapped[str] = mapped_column(String, nullable=False)
    document_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rendered_text: Mapped[str] = mapped_column(String, nullable=False)
    render_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'draft'"))
    source_watermark: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))
    published_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)


class UserInteractionContractItem(Base):
    """계약 항목 1건. 사용자 raw directive를 그대로 저장·주입하지 않는다 — typed 값 + 서버 템플릿 렌더."""

    __tablename__ = "user_interaction_contract_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    item_key: Mapped[str] = mapped_column(String, nullable=False)
    section: Mapped[str] = mapped_column(String, nullable=False)
    value_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rendered_text: Mapped[str] = mapped_column(String, nullable=False)
    # explicit_user | confirmed | repeated_observation — 캐피의 단일 추측은 publish되지 않는다.
    authority: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective_from: Mapped[datetime] = mapped_column(
        _TZ, nullable=False, server_default=text("now()")
    )
    effective_to: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class UserRelationshipState(Base):
    """결정적 관계 상태. LLM이 "가까워진 것 같다"고 썼다는 이유로 단계가 오르지 않는다(7절).

    `version`은 CAS용이고 `prompt_revision`은 stable prefix 교체 트리거다. 매 턴 바뀌는 counter가
    prompt_revision을 올리면 프롬프트 캐시가 매 턴 깨진다 — 그래서 분리한다.
    """

    __tablename__ = "user_relationship_states"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    relationship_started_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    active_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    successful_turns: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    qualifying_turns: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    last_interaction_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    relationship_stage: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'new'")
    )
    stage_rule_version: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'relationship-v1'")
    )
    latest_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    prompt_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class RelationshipEvent(Base):
    """append-only 관계 이벤트. 단계는 이 집합에서 **재계산 가능**해야 한다."""

    __tablename__ = "relationship_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    activity_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    delta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    dedup_key: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class LegacyRecallTombstone(Base):
    """과거에 사용자가 잊어달라고 한 source의 비노출 표식.

    ⚠️ 새 대화형 forget 기능이 **아니다.** legacy forget marker/closure/suppression을 source 단위로
    한 번 이관해, v2 backfill·mem0 후보·timeline·diary hydrate가 그 내용을 되살리지 않게 하는
    전환 장벽이다(14.3절).
    """

    __tablename__ = "legacy_recall_tombstones"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    diary_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


class ProviderBackoff(Base):
    """provider/model/lane 공유 backoff. 한 host의 429가 다른 host의 재시도 폭풍을 막는다(13.2절)."""

    __tablename__ = "provider_backoffs"

    provider: Mapped[str] = mapped_column(String, primary_key=True)
    model: Mapped[str] = mapped_column(String, primary_key=True)
    lane: Mapped[str] = mapped_column(String, primary_key=True)
    blocked_until: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=text("now()"))


__all__ = [
    "BOOTSTRAP_COLLECTING",
    "BOOTSTRAP_LEGACY",
    "BOOTSTRAP_READY",
    "MODE_LEGACY",
    "MODE_SHADOW",
    "MODE_V2",
    "LegacyRecallTombstone",
    "Mem0IngestCandidate",
    "Mem0IngestCandidateSource",
    "Mem0MemoryRegistry",
    "Mem0MemorySource",
    "MemoryPipelineState",
    "ProviderBackoff",
    "RelationshipEvent",
    "UserInteractionContract",
    "UserInteractionContractItem",
    "UserRelationshipState",
]
