"""shadow 계측 잡 — 대화에 영향을 주지 않으면서 배치를 잰다(15장 9번).

이 잡의 유일한 안전 요건은 **live chat 경로를 건드리지 않는 것**이다. 계측이 실패해도
사용자는 아무 영향을 받지 않아야 한다.
"""
from __future__ import annotations

import ast
import inspect

from worker import consumer, shadow_trace_jobs as st


def test_handler_is_registered():
    consumer._register_handlers()
    assert consumer._REGISTRY.get(st.JOB_SHADOW_TRACE) is st.handle_shadow_trace


def test_trace_job_never_calls_an_llm():
    """계측은 프롬프트를 **만들기만** 한다. 보내면 응답에 영향을 주고 비용도 든다."""
    src = inspect.getsource(st)
    tree = ast.parse(src)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"generate", "generate_step", "send"}
    ]
    assert calls == [], "계측 잡이 외부 모델을 호출한다"


def test_runs_on_the_maintenance_queue():
    """content 큐를 쓰면 계측이 기억 색인을 밀어낸다."""
    from app.services import jobs, memory_pipeline

    src = inspect.getsource(memory_pipeline.enqueue_shadow_trace)
    assert "QUEUE_MAINTENANCE" in src
    assert jobs.QUEUE_MAINTENANCE != jobs.QUEUE_CONTENT


def test_persona_comes_from_the_single_source():
    """페르소나를 여기서 다시 적으면 코드가 단일 소스라는 규칙이 깨진다."""
    src = inspect.getsource(st._build_segments)
    assert "prompts.system_prompt" in src
    assert "카피바라" not in src, "페르소나 원문이 복제됐다"


def test_recent_messages_are_append_only_class():
    """최근 원문이 휘발로 분류되면 캐시 가능 프리픽스가 사라진다."""
    from app.services import prompt_assembly as pa

    seg = pa.PromptSegment(pa.SegmentKind.RECENT, "user", "안녕")
    assert seg.cache_class is pa.CacheClass.APPEND_ONLY


def test_memory_and_snapshot_are_volatile():
    """mem0·서버 snapshot이 안정으로 분류되면 캐시가 매 턴 깨진다."""
    from app.services import prompt_assembly as pa

    for kind in (pa.SegmentKind.MEMORY, pa.SegmentKind.SERVER_SNAPSHOT):
        assert pa.PromptSegment(kind, "system", "x").cache_class is pa.CacheClass.CURRENT


def test_sql_uses_cast_not_double_colon():
    """SQLAlchemy text()에서 `::jsonb`는 바인드 파라미터로 오인돼 문법 오류가 난다(실측)."""
    src = inspect.getsource(st)
    assert "::jsonb" not in src
    assert "CAST(:counts AS jsonb)" in src
