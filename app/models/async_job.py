"""async_jobs — 내구 잡 플랫폼(W7)의 단일 테이블.

producer가 `(job_type, dedup_key)` 멱등 키로 enqueue → consumer가 큐별 슬롯으로 claim·실행·finalize.
상태 전이는 ready→running→(succeeded|ready(재시도)|dead|cancelled)뿐이고 terminal에서 같은 행을
ready로 되살리지 않는다(운영 replay는 새 행). 로직·SQL은 `app/services/jobs.py`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AsyncJob(Base):
    __tablename__ = "async_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # critical | interactive_async | content | notification | maintenance (스키마가 아닌 컬럼 값)
    queue: Mapped[str] = mapped_column(String, nullable=False)
    job_type: Mapped[str] = mapped_column(String, nullable=False)
    # 유저 삭제 시 CASCADE. 유저 무관 잡(maintenance 등)은 NULL.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    dedup_key: Mapped[str] = mapped_column(String, nullable=False)  # UNIQUE(job_type, dedup_key)
    replay_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("async_jobs.id"), nullable=True
    )
    replay_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payload_schema_version: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'job-payload-v1'")
    )
    payload_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload_redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'ready'"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # claim 시점에 증가(finalize 아님) — 크래시로 finalize 못 한 잡도 반드시 dead에 도달한다.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    # lease 3종은 running일 때만 존재(테이블 CHECK). finalize/heartbeat의 fencing 조건.
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_code: Mapped[str | None] = mapped_column(String, nullable=True)
    result_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
