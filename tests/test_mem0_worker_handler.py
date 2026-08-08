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

        async def scalar(self, *a, **k):
            return 0  # repair_generation

    async def _fake_enqueue(session, **kw):
        captured.update(kw)
        return None

    import app.services.jobs as jobs_mod
    orig = jobs_mod.enqueue
    jobs_mod.enqueue = _fake_enqueue
    try:
        await memory_pipeline.enqueue_ingest(
            _S(), _uuid.uuid4(), turn_seq=7, cursor=6, delay_s=180.0)
        assert captured["available_at"] is not None
        assert captured["available_at"] > datetime.now(timezone.utc)

        captured.clear()
        await memory_pipeline.enqueue_ingest(_S(), _uuid.uuid4(), turn_seq=7, cursor=6, delay_s=0)
        assert captured["available_at"] is None  # 0이면 예전처럼 즉시
    finally:
        jobs_mod.enqueue = orig


def test_resume_plan_is_scoped_to_the_same_repair_generation():
    """세대를 안 보면 지난 세대의 후보를 '앞 시도의 계획'으로 착각해 추출을 건너뛴다.

    2026-08-08 실측: 재추출한 4명 중 2명이 기억 0건이 됐다. 잡은 성공으로 찍히는데
    옛 후보를 그대로 다시 올리고, registry는 `ON CONFLICT DO NOTHING`에 걸려 아무것도
    안 남았다.
    """
    from worker import mem0_jobs

    sql = str(mem0_jobs._RESUME_PLAN)
    assert "repair_generation = :generation" in sql
    # 계획을 저장할 때도 같은 세대를 적어야 짝이 맞는다.
    assert ":generation" in str(mem0_jobs._STAGE_CANDIDATE)
    # 반대로 판정이 읽는 본문은 **세대로 거르면 안 된다.** provider id에 이미 세대가 들어
    # 있어 거를 필요가 없고, 거르면 지난 세대의 미판정 기억이 영원히 판정을 못 받는다.
    assert "repair_generation" not in str(mem0_jobs._CANDIDATE_TEXTS)


def test_all_memory_job_keys_use_one_generation_source():
    """추출·판정·삭제 키가 서로 다른 세대를 쓰면 한쪽만 새 키를 받는다.

    추출은 다시 도는데 판정 잡이 옛 키에 막히면, 그 기억은 `pending`인 채로 영원히
    안 보인다.
    """
    import inspect
    from app.services import chat, memory_pipeline

    # 세 dedup 키 모두 generation을 받는다.
    for fn in (memory_pipeline.ingest_dedup_key, memory_pipeline.consolidate_dedup_key,
               memory_pipeline.provider_delete_dedup_key):
        assert "generation" in inspect.signature(fn).parameters

    # 판정·삭제는 repair_generation을 직접 읽고, 추출은 호출측이 같은 값을 넘긴다.
    assert "repair_generation" in inspect.getsource(chat._record_memory_v2)
    assert "state.revision" not in inspect.getsource(chat._record_memory_v2)


def test_reextract_script_key_matches_the_real_key_builder():
    """스크립트가 손으로 만든 키가 실제 함수와 어긋나면 같은 턴을 두 번 처리한다."""
    import re
    import uuid as _uuid
    from pathlib import Path
    from app.services import memory_pipeline

    src = Path("scripts/reextract_memories.py").read_text()
    m = re.search(r'key = f"([^"]+)"', src)
    assert m, "스크립트에서 키 문자열을 못 찾았다"
    uid, gen = _uuid.uuid4(), 3
    built = m.group(1).replace("{uid}", str(uid)).replace("{gen}", str(gen))
    # 스크립트는 커서를 0으로 내린 뒤 첫 잡을 건다 — 키 기준도 커서 0이다.
    assert built == memory_pipeline.ingest_dedup_key(uid, 0, generation=gen)


def test_generation_suffix_cannot_collide_with_the_old_revision_keys():
    """접미사를 숫자만 붙이면 옛 revision 키(운영에 2·3·5가 남아 있다)와 같아진다."""
    import uuid as _uuid
    from app.services import memory_pipeline

    uid = _uuid.uuid4()
    # 키의 기준은 **출발 커서**다(`c` 접두어). 처리 대상 턴이 아니다.
    assert memory_pipeline.ingest_dedup_key(uid, 7, generation=0) == f"mem0:{uid}:c7:v1"
    for gen in (1, 2, 3, 5):
        key = memory_pipeline.ingest_dedup_key(uid, 7, generation=gen)
        assert not key.endswith(f":{gen}"), "숫자만 붙이면 옛 키와 겹친다"
        assert key.endswith(f":g{gen}")


# ── 사슬이 조용히 멈추지 않아야 한다 (2026-08-08 감사) ────────
def test_chunk_upper_is_bounded_by_the_source_cursor():
    """source를 넘는 턴까지 먹으면 커서 전진이 조용히 0행이 되고 그 사용자가 멈춘다."""
    import inspect
    src = inspect.getsource(mem0_jobs.handle_mem0_ingest)
    assert "upper=int(state.source_through_turn_seq)" in src
    assert "max(turn_seq" not in src, "payload 턴으로 상한을 넓히면 안 된다"


def test_cursor_advance_checks_the_generation():
    """재추출이 커서를 되돌린 뒤 옛 잡이 끝나면 커서를 다시 밀어 올려 구간을 건너뛴다."""
    from app.services import memory_pipeline
    assert "repair_generation = :generation" in str(memory_pipeline._ADVANCE_INGEST)


def test_next_ingest_falls_back_when_the_key_is_taken():
    """사슬의 유일한 연결 고리다 — 키에 막혀 조용히 끊기면 그 사람 기억이 영원히 멈춘다."""
    import inspect
    from app.services import memory_pipeline
    src = inspect.getsource(memory_pipeline.enqueue_next_ingest)
    assert "if made is None" in src, "enqueue 결과를 확인해야 한다"
    assert "dedup_key=f" in src, "막혔으면 유일 키로 다시 걸어야 한다"


def test_sweep_detects_stalls_by_user_cursor_not_only_dead_jobs():
    """실제 정지는 대부분 잡이 성공했는데 다음 잡이 안 생긴 경우다 — 죽은 잡 행이 없다."""
    from worker import memory_sweep_jobs as sw
    sql = str(sw._STALLED_USERS)
    assert "ingest_through_turn_seq < s.source_through_turn_seq" in sql
    assert "j.state IN ('ready', 'running')" in sql
    # 판정만 죽은 경우도 따로 본다(추출 커서는 따라잡아 위 조건에 안 걸린다).
    assert "semantic_status = 'pending'" in str(sw._UNJUDGED_USERS)


def test_reextract_jobs_run_behind_live_users():
    """처리 순서가 priority 오름차순이라, 기본값이면 재추출이 실사용자보다 먼저 처리된다."""
    from pathlib import Path
    src = Path("scripts/reextract_memories.py").read_text()
    assert "'ready',500,now(),8" in src, "재추출 잡은 우선순위를 뒤로 미룬다"
