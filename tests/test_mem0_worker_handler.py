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


def test_chunk_boundary_cuts_on_day_gap_and_cap():
    """덩어리는 활동일·간격·턴수 셋 중 먼저 걸리는 곳에서 끊긴다.

    활동일 조건이 빠지면 하루 경계 뒷정리(관계 투영·약속 추출·재판정)의 유일한 진입 경로가
    막힌다 — 덩어리가 자정을 넘으면 끝 턴과 그 앞 턴이 같은 날이라 경계가 안 보인다.
    """
    sql = str(mem0_jobs._CHUNK_BOUNDS)
    assert "day_changed" in sql                      # 활동일이 바뀌면 끊는다
    assert "make_interval(secs => :idle_s)" in sql   # 간격이 벌어지면 끊는다
    assert ":max_turns" in sql                       # 턴 수 상한
    assert "first_turn" in sql and "last_turn" in sql


def test_day_boundary_gets_the_first_turn_of_the_chunk():
    """끝 턴을 주면 덩어리 안이 전부 같은 날이라 하루가 닫힌 걸 못 본다."""
    import inspect
    src = inspect.getsource(mem0_jobs.handle_mem0_ingest)
    advance = src.split("async def _advance")[1]
    assert "enqueue_day_boundary_jobs" in advance
    call = advance.split("enqueue_day_boundary_jobs")[1][:120]
    assert "turn_seq=first_turn" in call, "덩어리의 첫 턴을 넘겨야 한다"


def test_chunk_is_frozen_into_the_payload():
    """재시도 때 구간이 움직이면 앞 시도의 계획을 못 이어받아 추출을 다시 부른다."""
    import inspect
    src = inspect.getsource(mem0_jobs.handle_mem0_ingest)
    assert 'payload.get("chunk")' in src, "고정된 구간을 먼저 본다"
    assert "freeze_job_payload" in src, "첫 시도가 구간을 적어둔다"


@pytest.mark.parametrize("turns,expected", [(1, 5), (2, 10), (4, 20), (5, 24), (20, 24)])
def test_candidate_limit_follows_chunk_size(turns, expected):
    """턴당 5개 상한을 그대로 두면 20턴 덩어리에서도 5개만 남아 뒷부분이 조용히 잘린다."""
    from app.services import mem0_ingest as mi
    assert mi.candidate_limit(turns) == expected


def test_prompt_and_code_caps_agree():
    """프롬프트가 코드 상한보다 크면 모델이 더 내고 뒷부분이 조용히 잘린다."""
    from app.services import mem0_extractor as me, mem0_ingest as mi
    assert me.MAX_CANDIDATES == mi.MAX_CANDIDATES_PER_CHUNK


def test_prompt_states_the_candidate_cap():
    """개수 지시가 없으면 모델이 몇 개를 낼지 알 수 없고, 넘치면 출력이 잘려 전량 폐기된다."""
    from app.services import mem0_extractor as me
    assert f"최대 {me.MAX_CANDIDATES}개" in me.build_system("ko")


def test_output_token_cap_fits_the_candidate_cap():
    """후보 하나가 40~80토큰이라 상한 개수를 담을 여유가 있어야 한다.

    모자라면 JSON이 안 닫혀 후보가 전량 폐기되고, 같은 입력으로 8번 재시도해 잡이 죽는다.
    """
    from app.services import mem0_extractor as me
    assert me.MAX_OUTPUT_TOKENS >= me.MAX_CANDIDATES * 80


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
