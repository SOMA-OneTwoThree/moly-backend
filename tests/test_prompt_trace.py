"""shadow trace — 배치가 캐시 구조에 미치는 영향을 수치로 고정한다.

이 설계의 비용 주장이 성립하는지 판정하는 테스트다. 실 dev 대화 40턴 실측에서
올바른 배치 99.1% 캐시가능 / 금지 배치 65.0%, 상대 비용 3.85배였다.
"""
from __future__ import annotations

from app.services import prompt_trace as pt
from app.services.prompt_assembly import PromptSegment, SegmentKind, assemble


def _seg(kind, content, role=None):
    default = {
        SegmentKind.RECENT: "user",
        SegmentKind.CURRENT_INPUT: "user",
    }.get(kind, "system")
    return PromptSegment(kind=kind, role=role or default, content=content)


def _build(volatile_first: bool):
    stable = [
        _seg(SegmentKind.PERSONA, "페" * 1000),
        _seg(SegmentKind.CONTRACT, "계" * 100),
    ]
    recent = [_seg(SegmentKind.RECENT, f"대화{i}" * 50) for i in range(20)]
    volatile = [
        _seg(SegmentKind.SERVER_SNAPSHOT, "상태" * 20),
        _seg(SegmentKind.MEMORY, "기억" * 20),
    ]
    current = [_seg(SegmentKind.CURRENT_INPUT, "이번 말")]
    return stable + (volatile + recent if volatile_first else recent + volatile) + current


def test_correct_ordering_keeps_conversation_cacheable():
    t = pt.trace(assemble(_build(False)))
    assert t.cacheable_ratio > 0.9, "대화 이력이 캐시 프리픽스에 포함돼야 한다"


def test_volatile_first_destroys_conversation_cache():
    """휘발값이 앞에 오면 그 뒤 대화 전체가 매 턴 새로 청구된다."""
    t = pt.trace(_build(True))
    assert t.cacheable_ratio < 0.5


def test_cost_multiplier_is_material():
    """비용 차이가 무시할 수준이 아님을 수치로 고정한다."""
    got = pt.compare_orderings(assemble(_build(False)), _build(True))
    assert got["cost_multiplier"] >= 2.0
    assert got["good_cacheable_ratio"] > got["bad_cacheable_ratio"]


def test_longer_conversation_widens_the_gap():
    """대화가 길수록 잘못된 배치의 손해가 커진다."""

    def multiplier(n_turns: int) -> float:
        stable = [_seg(SegmentKind.PERSONA, "페" * 500)]
        recent = [_seg(SegmentKind.RECENT, "대화" * 50) for _ in range(n_turns)]
        vol = [_seg(SegmentKind.MEMORY, "기억" * 20)]
        cur = [_seg(SegmentKind.CURRENT_INPUT, "말")]
        good = assemble(stable + recent + vol + cur)
        bad = stable + vol + recent + cur
        return pt.compare_orderings(good, bad)["cost_multiplier"]

    assert multiplier(40) > multiplier(5)


def test_trace_counts_utf8_bytes_not_characters():
    t = pt.trace(assemble([
        _seg(SegmentKind.PERSONA, "가"),          # 3 bytes
        _seg(SegmentKind.CURRENT_INPUT, "나"),    # 3 bytes
    ]))
    assert t.total_bytes == 6


def test_weighted_cost_applies_cache_discount():
    t = pt.PromptTrace(total_bytes=1000, cacheable_bytes=900, volatile_bytes=100, message_count=2)
    # 900×0.1 + 100 = 190
    assert t.weighted_cost_units() == 190
