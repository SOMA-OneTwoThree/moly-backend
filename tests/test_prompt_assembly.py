"""프롬프트 조립 v2 — 고정 순서와 캐시 경계.

이 파일이 지키는 단 하나: **휘발 컨텍스트는 최근 원문 뒤에 온다.**
앞에 오면 그 뒤의 append-only 대화가 전부 cache miss가 되어 턴당 비용이 몇 배로 뛴다.
"""
from __future__ import annotations

import pytest

from app.services.prompt_assembly import (
    CacheClass,
    PromptOrderError,
    PromptSegment,
    SegmentKind,
    assemble,
    prompt_bytes,
    stable_prefix,
    to_openai_messages,
)


def _seg(kind, content="x", role=None):
    default_role = {
        SegmentKind.RECENT: "user",
        SegmentKind.CURRENT_INPUT: "user",
        SegmentKind.TOOL_RESULT: "tool",
    }.get(kind, "system")
    return PromptSegment(kind=kind, role=role or default_role, content=content)


def _full():
    return [
        _seg(SegmentKind.PERSONA, "페르소나"),
        _seg(SegmentKind.CONTRACT, "계약"),
        _seg(SegmentKind.RELATIONSHIP, "관계"),
        _seg(SegmentKind.RECENT, "지난 말", role="user"),
        _seg(SegmentKind.SERVER_SNAPSHOT, "지금 상태"),
        _seg(SegmentKind.MEMORY, "기억"),
        _seg(SegmentKind.CURRENT_INPUT, "이번 말", role="user"),
    ]


# ── 순서 ─────────────────────────────────────────────────────
def test_volatile_context_comes_after_recent_history():
    """이 설계의 핵심 — 순서가 뒤집히면 대화 배열 전체가 매 턴 미캐시가 된다."""
    ordered = assemble(_full())
    kinds = [s.kind for s in ordered]
    assert kinds.index(SegmentKind.RECENT) < kinds.index(SegmentKind.SERVER_SNAPSHOT)
    assert kinds.index(SegmentKind.RECENT) < kinds.index(SegmentKind.MEMORY)


def test_current_input_is_last_before_tool_results():
    ordered = assemble([*_full(), _seg(SegmentKind.TOOL_RESULT, "도구", role="tool")])
    kinds = [s.kind for s in ordered]
    assert kinds[-2] == SegmentKind.CURRENT_INPUT
    assert kinds[-1] == SegmentKind.TOOL_RESULT


def test_caller_order_is_corrected_not_trusted():
    """호출측이 뒤섞어 줘도 cache class 순으로 정렬된다."""
    shuffled = list(reversed(_full()))
    ordered = assemble(shuffled)
    assert [s.cache_class.value for s in ordered] == sorted(
        s.cache_class.value for s in ordered
    )


def test_same_class_keeps_given_order():
    """안정 정렬 — 같은 class 안에서는 준 순서를 지킨다(페르소나가 계약보다 앞)."""
    ordered = assemble(_full())
    stable = [s.kind for s in ordered if s.cache_class is CacheClass.STABLE]
    assert stable == [SegmentKind.PERSONA, SegmentKind.CONTRACT, SegmentKind.RELATIONSHIP]


# ── 분류 강제 ────────────────────────────────────────────────
def test_kind_determines_cache_class_not_the_caller():
    """mem0를 STABLE로 선언해 프리픽스에 밀어넣는 실수가 불가능해야 한다."""
    assert _seg(SegmentKind.MEMORY).cache_class is CacheClass.CURRENT
    assert _seg(SegmentKind.PERSONA).cache_class is CacheClass.STABLE
    assert _seg(SegmentKind.RECENT).cache_class is CacheClass.APPEND_ONLY


def test_untrusted_kinds_are_marked():
    assert _seg(SegmentKind.MEMORY).untrusted is True
    assert _seg(SegmentKind.CHECKPOINT).untrusted is True
    assert _seg(SegmentKind.PERSONA).untrusted is False
    assert _seg(SegmentKind.CONTRACT).untrusted is False  # 계약은 따라야 할 지시다


def test_missing_stable_prefix_is_rejected():
    with pytest.raises(PromptOrderError):
        assemble([_seg(SegmentKind.CURRENT_INPUT, "말", role="user")])


def test_empty_is_rejected():
    with pytest.raises(PromptOrderError):
        assemble([])


# ── 직렬화 ───────────────────────────────────────────────────
def test_serializer_does_not_collapse_everything_into_one_system():
    """기존 serializer의 '단일 system 선두 고정'이면 새 순서를 표현할 수 없다."""
    msgs = to_openai_messages(assemble(_full()))
    assert msgs[0]["role"] == "system"          # stable 묶음
    assert "페르소나" in msgs[0]["content"] and "계약" in msgs[0]["content"]
    # 최근 원문이 stable 바로 뒤, 휘발 컨텍스트는 그 뒤에 별도 메시지로.
    assert msgs[1] == {"role": "user", "content": "지난 말"}
    assert msgs[2]["content"] == "지금 상태"
    assert msgs[-1] == {"role": "user", "content": "이번 말"}


def test_only_stable_segments_are_merged():
    msgs = to_openai_messages(assemble(_full()))
    systems = [m for m in msgs if m["role"] == "system"]
    # stable 1개 + current 2개(snapshot, memory) — current끼리는 합치지 않는다.
    assert len(systems) == 3
    assert sum(1 for m in msgs if m["content"] == "지금 상태") == 1


def test_stable_prefix_selection():
    ordered = assemble(_full())
    assert [s.kind for s in stable_prefix(ordered)] == [
        SegmentKind.PERSONA, SegmentKind.CONTRACT, SegmentKind.RELATIONSHIP
    ]


def test_prompt_bytes_counts_utf8():
    ordered = assemble(_full())
    assert prompt_bytes(ordered) == sum(
        len(m["content"].encode("utf-8")) for m in to_openai_messages(ordered)
    )
    assert prompt_bytes(ordered) > 0
