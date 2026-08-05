"""checkpoint v2 shadow 생성 — live 프롬프트를 바꾸지 않는다(15장 8번).

shadow의 요점은 **비교 산출물을 만들되 아무것도 활성화하지 않는 것**이다. 여기서 published가
하나라도 나오면 사용자가 보는 대화 맥락이 조용히 바뀐다.
"""
from __future__ import annotations

import ast
import inspect
from datetime import date

import pytest

from app.services import checkpoint_v2 as cv2
from worker import consumer, shadow_checkpoint_jobs as sc


def test_handler_is_registered():
    consumer._register_handlers()
    assert consumer._REGISTRY.get(sc.JOB_SHADOW_CHECKPOINT) is sc.handle_shadow_checkpoint


def test_everything_is_written_as_ready_never_published():
    """'ready'가 아니면 anchor가 줄어 원문이 프롬프트에서 빠진다(8.3절)."""
    src = inspect.getsource(sc)
    assert "'ready'" in src
    assert "PUBLISH_PUBLISHED" not in src, "shadow가 published를 쓴다"


def test_reuses_the_existing_summarizer():
    """별도 요약 경로를 만들면 실명 누출 검사와 마스킹이 빠진다."""
    tree = ast.parse(inspect.getsource(sc))
    calls = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "summarize" in calls
    assert "generate" not in calls, "요약기를 우회해 모델을 직접 부른다"


def test_name_leak_is_retried_not_stored():
    """실명이 남은 요약을 저장하면 그 사고가 영구화된다."""
    src = inspect.getsource(sc.handle_shadow_checkpoint)
    assert "NameLeakError" in src
    idx = src.index("NameLeakError")
    assert "JobRetry" in src[idx:idx + 300]


def test_digest_must_be_a_single_day():
    """digest가 여러 날을 덮으면 시간 회상이 어긋난다."""
    r = cv2.digest_ranges(segment_from=1, segment_through=9)
    row = cv2.CheckpointRow(
        kind=cv2.KIND_DAILY_DIGEST, ranges=r, previous_checkpoint_id=None,
        summary="요약", source_hash="h", locale="ko",
        source_started_at=None, source_ended_at=None,
        activity_date_from=date(2026, 8, 5), activity_date_to=date(2026, 8, 6),
    )
    with pytest.raises(cv2.CheckpointContractError):
        cv2.validate(row)


def test_digest_never_joins_the_window_chain():
    """하루 요약이 체인에 붙으면 장기 사실로 되먹는다."""
    r = cv2.digest_ranges(segment_from=1, segment_through=9)
    row = cv2.CheckpointRow(
        kind=cv2.KIND_DAILY_DIGEST, ranges=r, previous_checkpoint_id="some-id",
        summary="요약", source_hash="h", locale="ko",
        source_started_at=None, source_ended_at=None,
        activity_date_from=date(2026, 8, 5), activity_date_to=date(2026, 8, 5),
    )
    with pytest.raises(cv2.CheckpointContractError):
        cv2.validate(row)


def test_window_coverage_accumulates_across_the_chain():
    """coverage가 누적되지 않으면 오래된 줄거리가 사라진다."""
    first = cv2.window_ranges(segment_from=1, segment_through=10, previous=None)
    assert first.coverage_from == 1 and first.coverage_through == 10
    second = cv2.window_ranges(segment_from=11, segment_through=20, previous=first)
    assert second.coverage_from == 1, "체인 시작을 잃었다"
    assert second.coverage_through == 20


def test_range_contract_violation_is_fatal_not_silently_fixed():
    """조용히 보정하면 사이 구간이 어디에도 요약되지 않은 채 anchor만 전진한다."""
    src = inspect.getsource(sc.handle_shadow_checkpoint)
    assert "range_contract" in src and "JobFatal" in src
