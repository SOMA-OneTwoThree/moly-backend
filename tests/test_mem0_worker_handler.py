"""mem0 ingest handler — 처리 조건과 등록.

bootstrap이 끝나기 전 live turn을 색인하면 cursor 연속성이 깨진다. 그 가드가 handler에 있어야 한다.
"""
from __future__ import annotations

import pytest

from worker import consumer, mem0_jobs


def test_handler_is_registered():
    consumer._register_handlers()
    assert mem0_jobs.JOB_MEM0_INGEST in consumer.registered_types()


def test_handler_registered_on_the_live_consumer_module():
    """PR #91의 이중 모듈 적재 회귀 방지 — 실제 registry에 들어가야 한다."""
    assert consumer._REGISTRY.get(mem0_jobs.JOB_MEM0_INGEST) is mem0_jobs.handle_mem0_ingest


async def test_invalid_payload_is_fatal_not_retried():
    """payload가 깨진 건 재시도해도 같다 — 즉시 dead."""
    from app.services.jobs import ClaimedJob
    import uuid
    from datetime import datetime, timedelta, timezone

    job = ClaimedJob(
        id=uuid.uuid4(), queue="content", job_type=mem0_jobs.JOB_MEM0_INGEST,
        user_id=uuid.uuid4(), dedup_key="d", payload={}, attempt=1, max_attempts=3,
        lease_owner="W", lease_token=uuid.uuid4(),
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=60), expires_at=None,
    )
    with pytest.raises(consumer.JobFatal):
        await mem0_jobs.handle_mem0_ingest(job)


def test_collection_version_is_pinned():
    """런타임이 컬렉션을 만들지 않는다 — migration이 만든 버전만 쓴다."""
    assert mem0_jobs.COLLECTION_VERSION == "v2"
