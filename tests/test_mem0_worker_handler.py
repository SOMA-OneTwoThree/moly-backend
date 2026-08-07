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


def test_no_memory_turn_advances_consolidated_cursor_directly():
    """기억 0건 turn은 consolidate 잡이 없다 — 여기서 커서를 통과시키지 않으면
    consolidated 커서가 영원히 걸려 cutover gate를 절대 통과할 수 없다(dev 실측)."""
    import inspect

    src = inspect.getsource(mem0_jobs.handle_mem0_ingest)
    advance = src.split("async def _advance")[1]
    assert "enqueue_consolidate" in advance
    assert "advance_consolidated_cursor" in advance
    # 판정할 게 있으면 잡을 만들고, 없으면 커서만 통과 — 둘 다 있어야 한다.
    assert "else:" in advance


def test_provider_delete_handler_is_registered():
    consumer._register_handlers()
    assert mem0_jobs.JOB_MEM0_PROVIDER_DELETE in consumer.registered_types()


def test_delete_only_targets_non_active_memories():
    """semantic 상태가 먼저 확정된 뒤에야 지운다(9.4절 6번)."""
    sql = str(mem0_jobs._DELETE_TARGETS)
    assert "semantic_status IN ('duplicate','superseded','excluded','rejected_policy')" in sql
    assert "provider_delete_state = 'pending'" in sql
    assert "LIMIT :limit" in sql  # bounded


def test_delete_failure_of_one_does_not_block_others():
    """개별 실패가 나머지 정리를 막지 않는다 — 삭제는 정합성이 아니라 저장 비용 문제다."""
    import inspect

    src = inspect.getsource(mem0_jobs.handle_mem0_provider_delete)
    assert "failed.append" in src and "deleted.append" in src


# ── 대화 덩어리 단위 추출 (2026-08-08) ──────────────────────────────────
#
# 턴 하나만 보면 한 사건이 여러 턴에 걸칠 때 조각난다. 실제로 유저가 캐피 모습을 보고
# "귀여워"라고 한 턴에서 무엇을 귀여워했는지가 빠졌다. 실측으로 유저 발화의 86%가 3분 안에
# 이어져서, 잡을 늦춰 덩어리로 묶고 그 구간을 한 번에 먹게 바꿨다.

def test_source_query_reads_a_range_not_one_turn():
    """`turn_seq = :turn_seq`로 되돌아가면 다시 조각난다."""
    sql = str(mem0_jobs._SOURCE_MESSAGES)
    assert "turn_seq > :from_seq" in sql and "turn_seq <= :to_seq" in sql
    assert "turn_seq = :turn_seq" not in sql


@pytest.mark.parametrize(
    "payload_turn,ingest,source,expected",
    [
        # 대화가 이어져 커서(10)와 현재(15) 사이가 벌어졌다 — 한 번에 먹는다
        (11, 10, 15, (10, 15)),
        # 한 턴만 쌓였으면 한 턴만
        (11, 10, 11, (10, 11)),
        # 상한(20턴)을 넘으면 잘라서 먹고 나머지는 다음 잡이 이어간다
        (11, 10, 500, (10, 30)),
        # source가 뒤처져 있어도 payload가 가리키는 턴까지는 먹는다
        (14, 10, 12, (10, 14)),
        # 구간이 비어도 최소 한 턴 — 안 그러면 커서가 안 움직여 같은 잡이 무한히 돈다
        (10, 10, 10, (10, 11)),
    ],
)
def test_ingest_window(payload_turn, ingest, source, expected):
    assert mem0_jobs.ingest_window(
        payload_turn=payload_turn, ingest_through=ingest,
        source_through=source, max_turns=20,
    ) == expected


def test_ingest_window_never_reprocesses_past_turns():
    """시작점은 항상 커서다 — 이미 처리한 턴을 다시 뽑으면 같은 기억이 중복으로 쌓인다."""
    from_seq, to_seq = mem0_jobs.ingest_window(
        payload_turn=3, ingest_through=100, source_through=105, max_turns=20)
    assert from_seq == 100 and to_seq > from_seq


async def test_enqueue_ingest_delays_so_the_chunk_can_accumulate():
    """지연이 없으면 턴마다 잡이 돌아 예전처럼 조각난다."""
    import uuid as _uuid
    from datetime import datetime, timezone
    from app.services import memory_pipeline

    captured = {}

    class _S:
        async def execute(self, *a, **k):
            class _R:
                def first(self_inner):
                    return None
            return _R()

    async def _fake_enqueue(session, **kw):
        captured.update(kw)
        return None

    import app.services.jobs as jobs_mod
    orig = jobs_mod.enqueue
    jobs_mod.enqueue = _fake_enqueue
    try:
        await memory_pipeline.enqueue_ingest(
            _S(), _uuid.uuid4(), turn_seq=7, delay_s=180.0)
        assert captured["available_at"] is not None
        assert captured["available_at"] > datetime.now(timezone.utc)

        captured.clear()
        await memory_pipeline.enqueue_ingest(_S(), _uuid.uuid4(), turn_seq=7, delay_s=0)
        assert captured["available_at"] is None  # 0이면 예전처럼 즉시
    finally:
        jobs_mod.enqueue = orig
