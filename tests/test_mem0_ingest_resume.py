"""ingest 재시도 수렴 — 앞 시도가 남긴 행을 고아로 만들지 않는다.

실제로 dev에서 난 사고다. 커서는 28/28/28로 다 따라잡았는데 turn 24에 판정 안 된 registry가
2건 남아 cutover gate가 영구히 막혔다. 원인은 두 가지가 겹친 것이다:

 1. 재시도가 `existing_plan` 없이 extractor(LLM)를 다시 굴렸다 — 결과가 달라졌다
 2. 커서 통과 여부를 **이번 실행의 outcome**으로만 판단했다 — 앞 시도의 pending을 못 봤다
"""
from __future__ import annotations

import uuid

import pytest

from app.services import mem0_ingest as mi
from app.services import mem0_pipeline
from app.services.mem0_budget import memory_ingest_budget


def _plan(text_: str) -> mem0_pipeline.PlannedCandidate:
    return mem0_pipeline.PlannedCandidate(
        provider_memory_id=uuid.uuid5(uuid.NAMESPACE_URL, text_),
        candidate_hash=mi.candidate_hash(text_),
        text=text_,
    )


@pytest.mark.asyncio
async def test_resume_does_not_call_extractor_again():
    """앞 계획이 있으면 LLM을 다시 부르지 않는다 — 부르면 다른 답이 나와 행이 갈린다."""
    calls = []

    async def extract(timeout):
        calls.append(1)
        return []

    async def embed(texts, timeout):
        return [[0.1] * 8 for _ in texts]

    staged, registered = [], []

    out = await mem0_pipeline.run_ingest(
        user_id=uuid.uuid4(), turn_seq=24, collection_version="v2",
        budget=memory_ingest_budget(total_s=60),
        extract=extract, embed=embed,
        upsert=lambda rows, timeout: _noop(rows),
        stage_planned=lambda p: _collect(staged, p),
        register_pending=lambda p: _collect(registered, p),
        existing_plan=[_plan("귤모자를 씌웠다")],
    )

    assert calls == [], "재시도가 extractor를 다시 불렀다"
    assert len(out.planned) == 1
    assert staged == [], "이어받은 계획을 다시 stage하면 안 된다"
    assert len(registered) == 1, "registry는 멱등하게 다시 확정돼야 한다"


@pytest.mark.asyncio
async def test_resumed_plan_keeps_same_provider_ids():
    """같은 계획은 같은 provider id로 수렴해야 중복 벡터가 안 생긴다."""
    p = _plan("처음 만난 날이 있다")
    seen = []

    async def embed(texts, timeout):
        return [[0.1] * 8 for _ in texts]

    async def upsert(rows, timeout):
        seen.extend(r[0] for r in rows)

    for _ in range(2):
        await mem0_pipeline.run_ingest(
            user_id=uuid.uuid4(), turn_seq=1, collection_version="v2",
            budget=memory_ingest_budget(total_s=60),
            extract=_never, embed=embed, upsert=upsert,
            stage_planned=_never_stage, register_pending=_never_stage,
            existing_plan=[p],
        )

    assert seen[0] == seen[1] == p.provider_memory_id


async def _noop(_rows):
    return None


async def _collect(sink, planned):
    sink.extend(planned)


async def _never(*a, **k):
    raise AssertionError("불려서는 안 되는 경로다")


async def _never_stage(_planned):
    return None


def test_cursor_decision_reads_db_not_this_runs_outcome():
    """커서 통과 판단이 `outcome.planned`면 앞 시도의 pending을 못 본다.

    dev 사고의 직접 원인이다. 판단 근거는 DB의 pending 수여야 한다.
    """
    import ast
    import inspect

    from worker import mem0_jobs

    src = inspect.getsource(mem0_jobs.handle_mem0_ingest)
    tree = ast.parse(src.lstrip())
    advance = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_advance"
    )
    branch = next(n for n in advance.body if isinstance(n, ast.If))
    test_src = ast.dump(branch.test)
    assert "planned" not in test_src, "이번 실행의 outcome으로 커서를 통과시키고 있다"
    assert "pending" in test_src


def test_ingest_passes_resumed_plan_to_pipeline():
    """`existing_plan`을 안 넘기면 재시도가 extractor를 다시 굴린다."""
    import ast
    import inspect

    from worker import mem0_jobs

    src = inspect.getsource(mem0_jobs.handle_mem0_ingest)
    tree = ast.parse(src.lstrip())
    call = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run_ingest"
    )
    assert "existing_plan" in {k.arg for k in call.keywords}
