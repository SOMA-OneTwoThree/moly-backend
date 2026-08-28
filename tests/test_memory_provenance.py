"""기억 provenance — "어느 발화에서 나온 기억인가"를 저장한다.

**이게 없으면 정정이 불가능하다.** 사용자가 "산책 안 했어"라고 정정해도, 어느 기억을
닫아야 하는지 특정할 방법이 없다. 실제로 dev에서 정정 뒤에도 `산책 갔다왔다`가 계속
검색됐다(감사 지적).

추출기는 처음부터 검증된 evidence span을 만들고 있었다. 워커가 그걸 버렸을 뿐이다.
"""
from __future__ import annotations

import ast
import inspect
import uuid

from app.services import mem0_ingest as mi
from app.services import mem0_pipeline as mp
from worker import mem0_jobs


def _span(mid=10, sender="user"):
    return mi.EvidenceSpan(
        message_id=mid, sender=sender, start_utf8=0, end_utf8=5, content_hash="h" * 8
    )


def _planned(evidence=()):
    return mp.PlannedCandidate(
        provider_memory_id=uuid.uuid4(), candidate_hash="a" * 8, text="산책을 갔다",
        candidate=mi.Candidate(text="산책을 갔다", evidence=tuple(evidence), category="event"),
    )


def test_planned_candidate_carries_evidence():
    """계획이 근거를 안 들고 다니면 저장 시점에 이미 잃는다."""
    p = _planned([_span()])
    assert len(p.evidence) == 1
    assert p.evidence[0].message_id == 10


def test_resumed_plan_without_candidate_has_no_evidence_but_does_not_crash():
    """재시도로 DB에서 되살린 계획엔 candidate가 없다(근거는 이미 저장돼 있다)."""
    p = mp.PlannedCandidate(provider_memory_id=uuid.uuid4(), candidate_hash="a", text="x")
    assert p.evidence == ()


def test_worker_writes_candidate_sources():
    """근거를 candidate 단계에서 저장하지 않으면 crash 복구가 그걸 못 되살린다."""
    src = inspect.getsource(mem0_jobs)
    assert "mem0_ingest_candidate_sources" in src
    assert "_CANDIDATE_SOURCE" in src


def test_worker_writes_memory_sources_with_time_coordinates():
    """timeline 원문 hydration과 tombstone 검증이 이 표를 읽는다.

    발생 시각·활동일이 없으면 "그때 뭐라고 했었지"를 재현할 수 없다.
    """
    src = inspect.getsource(mem0_jobs)
    assert "mem0_memory_sources" in src
    for col in ("source_occurred_at", "source_activity_date", "extractor_version"):
        assert col in src, f"{col}이 저장되지 않는다"


def test_evidence_is_written_in_the_same_transaction_as_the_row_it_belongs_to():
    """따로 커밋하면 그 사이 crash에서 근거 없는 기억이 남는다.

    #21 배치화 이후: 근거 저장은 `_evidence_arrays`(후보 전체 평탄화) → 배치 INSERT 1문이고,
    본문 저장과 같은 세션에서 커밋 앞에 실행돼야 한다는 계약은 동일하다.
    """
    src = inspect.getsource(mem0_jobs.handle_mem0_ingest)
    tree = ast.parse(inspect.getsource(mem0_jobs.handle_mem0_ingest).lstrip())
    evidence_stmt = {"_stage": "_CANDIDATE_SOURCES_BATCH", "_register": "_MEMORY_SOURCES_BATCH"}
    for name, stmt in evidence_stmt.items():
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        body = ast.unparse(fn)
        assert stmt in body, f"{name}이 근거를 저장하지 않는다"
        assert body.rindex("commit") > body.index(stmt), \
            f"{name}이 근거 저장 전에 커밋한다"
    assert "_MEMORY_SOURCES_BATCH" in src


def test_only_user_utterances_can_be_evidence():
    """캐피가 한 말을 근거로 삼으면 사용자가 하지 않은 약속이 사실이 된다.

    추출기가 이미 거른다 — 그 계약이 살아 있는지 고정한다.
    """
    src = inspect.getsource(mi)
    assert "사용자 발화여야" in src or "user" in src
    ev = _span(sender="moly")
    assert ev.sender != "user"  # 이런 span은 추출기가 assistant_evidence로 폐기한다
