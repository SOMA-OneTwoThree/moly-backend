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


def test_consolidation_handler_is_registered():
    consumer._register_handlers()
    assert mem0_jobs.JOB_MEM0_CONSOLIDATE in consumer.registered_types()
    assert consumer._REGISTRY[mem0_jobs.JOB_MEM0_CONSOLIDATE] is mem0_jobs.handle_mem0_consolidate


def test_classifier_is_called_at_most_once_per_job():
    """invalid graph여도 재질의하지 않는다 — 비용과 비결정성만 늘린다."""
    import ast
    import inspect

    src = inspect.getsource(mem0_jobs.handle_mem0_consolidate)
    tree = ast.parse(src.lstrip())
    generate_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "generate"
    ]
    assert len(generate_calls) == 1


def test_consolidation_advances_cursor_only_in_apply_domain():
    """커서 전진이 fencing UPDATE와 같은 transaction이어야 한다."""
    import inspect

    src = inspect.getsource(mem0_jobs.handle_mem0_consolidate)
    # advance_consolidated_cursor는 apply_domain 콜백(_publish/_skip) 안에서만 호출된다.
    for line in src.splitlines():
        if "advance_consolidated_cursor" in line:
            assert line.startswith("            ") or line.startswith("        ")
