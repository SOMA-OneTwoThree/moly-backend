"""checkpoint v2 — 누적 window와 독립 daily digest의 범위 계약.

여기서 지키는 것:
 · window는 누적이라 최신 한 건만 넣어도 오래된 줄거리가 사라지지 않는다
 · daily digest는 체인에 붙지 않는다 — 하루 요약이 장기 사실로 되먹지 않게
 · 구간이 끊기거나 겹치면 조용히 보정하지 않고 실패시킨다
 · anchor는 요약된 구간을 다시 붙잡지 않는다
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.services import checkpoint_v2 as cp

_T0 = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _row(**kw):
    base = dict(
        kind=cp.KIND_WINDOW,
        ranges=cp.Ranges(1, 10, 1, 10),
        previous_checkpoint_id=None,
        summary="줄거리",
        source_hash="h",
        locale="ko",
        source_started_at=_T0,
        source_ended_at=_T0,
        activity_date_from=date(2026, 8, 5),
        activity_date_to=date(2026, 8, 5),
    )
    base.update(kw)
    return cp.CheckpointRow(**base)


# ── window 누적 ──────────────────────────────────────────────
def test_first_window_coverage_equals_segment():
    r = cp.window_ranges(segment_from=1, segment_through=10, previous=None)
    assert r == cp.Ranges(1, 10, 1, 10)


def test_second_window_accumulates_coverage_from_the_chain_start():
    """최신 한 건만 프롬프트에 넣어도 첫 구간이 사라지지 않는다."""
    first = cp.window_ranges(segment_from=1, segment_through=10, previous=None)
    second = cp.window_ranges(segment_from=11, segment_through=20, previous=first)
    assert second.coverage_from == 1 and second.coverage_through == 20
    assert second.segment_from == 11  # 이번에 새로 요약한 구간은 따로 남는다


def test_gap_between_coverage_and_segment_is_allowed_only_forward():
    """id가 연속이 아닐 수 있다 — 앞으로 가는 gap은 정상."""
    first = cp.window_ranges(segment_from=1, segment_through=10, previous=None)
    second = cp.window_ranges(segment_from=17, segment_through=25, previous=first)
    assert second.coverage_from == 1 and second.coverage_through == 25


def test_overlapping_segment_is_rejected():
    """겹치면 같은 대화가 두 번 요약된다 — 보정하지 않고 실패."""
    first = cp.window_ranges(segment_from=1, segment_through=10, previous=None)
    with pytest.raises(cp.CheckpointContractError):
        cp.window_ranges(segment_from=10, segment_through=20, previous=first)


def test_inverted_segment_is_rejected():
    with pytest.raises(cp.CheckpointContractError):
        cp.window_ranges(segment_from=20, segment_through=10, previous=None)


# ── daily digest 독립성 ──────────────────────────────────────
def test_digest_does_not_accumulate():
    assert cp.digest_ranges(segment_from=5, segment_through=9) == cp.Ranges(5, 9, 5, 9)


def test_digest_cannot_link_to_window_chain():
    """하루 요약이 체인에 붙으면 장기 사실로 되먹는다."""
    with pytest.raises(cp.CheckpointContractError):
        cp.validate(_row(kind=cp.KIND_DAILY_DIGEST, previous_checkpoint_id="abc"))


def test_digest_must_cover_exactly_one_day():
    with pytest.raises(cp.CheckpointContractError):
        cp.validate(
            _row(
                kind=cp.KIND_DAILY_DIGEST,
                activity_date_from=date(2026, 8, 5),
                activity_date_to=date(2026, 8, 6),
            )
        )


def test_valid_digest_passes():
    cp.validate(_row(kind=cp.KIND_DAILY_DIGEST, ranges=cp.Ranges(3, 8, 3, 8)))


# ── 저장 전 계약 ─────────────────────────────────────────────
def test_window_coverage_end_must_match_segment_end():
    with pytest.raises(cp.CheckpointContractError):
        cp.validate(_row(ranges=cp.Ranges(11, 20, 1, 25)))


def test_empty_summary_is_rejected():
    with pytest.raises(cp.CheckpointContractError):
        cp.validate(_row(summary="   "))


def test_unknown_kind_is_rejected():
    with pytest.raises(cp.CheckpointContractError):
        cp.validate(_row(kind="something_else"))


# ── anchor ───────────────────────────────────────────────────
def test_anchor_must_be_after_coverage_through():
    """anchor가 요약 구간 안이면 같은 대화를 원문으로 또 붙잡는다."""
    row = _row(ranges=cp.Ranges(1, 20, 1, 20))
    assert cp.anchor_after(row, next_retained_message_id=21) == 21
    with pytest.raises(cp.CheckpointContractError):
        cp.anchor_after(row, next_retained_message_id=20)


def test_anchor_does_not_assume_plus_one():
    """전역 message id는 연속이 아니다 — 실제 다음 메시지를 그대로 쓴다."""
    row = _row(ranges=cp.Ranges(1, 20, 1, 20))
    assert cp.anchor_after(row, next_retained_message_id=37) == 37
