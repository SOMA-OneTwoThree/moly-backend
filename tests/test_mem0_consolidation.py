"""consolidation validator — 틀린 classifier 그래프가 기억을 조용히 망치지 않게.

mem0는 add-only라 활성 상태를 우리가 판정한다. classifier(LLM)는 틀릴 수 있고, 틀린 그래프를
일부만 적용하면 기억이 사라지거나 되살아난다. 그래서 위반은 component 통째로 ambiguous다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.services import mem0_consolidation as mc

_T0 = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _ref(n: int, *, is_new=True, turn=1, at=None, h=None) -> mc.MemoryRef:
    return mc.MemoryRef(
        registry_id=uuid.UUID(int=n),
        source_turn_seq=turn,
        candidate_hash=h or f"h{n}",
        source_occurred_at=at or _T0,
        is_new=is_new,
    )


def _status(result, n) -> str | None:
    for t in result.transitions:
        if t.registry_id == uuid.UUID(int=n):
            return t.semantic_status
    return None


# ── 정상 판정 ────────────────────────────────────────────────
def test_new_memory_without_edges_is_active():
    r = mc.consolidate([_ref(1)], [])
    assert _status(r, 1) == mc.STATUS_ACTIVE


def test_supersedes_closes_old_and_activates_new():
    refs = [_ref(1), _ref(2, is_new=False)]
    edges = [mc.Edge(uuid.UUID(int=1), uuid.UUID(int=2), mc.Verdict.SUPERSEDES)]
    r = mc.consolidate(refs, edges)
    assert _status(r, 1) == mc.STATUS_ACTIVE
    assert _status(r, 2) == mc.STATUS_SUPERSEDED
    old = next(t for t in r.transitions if t.registry_id == uuid.UUID(int=2))
    assert old.superseded_by == uuid.UUID(int=1)
    assert old.provider_delete_state == mc.DELETE_PENDING


def test_duplicate_keeps_existing_canonical():
    """기존 기억이 canonical — 새 중복은 닫고 provenance는 유지한다."""
    refs = [_ref(1), _ref(2, is_new=False)]
    edges = [mc.Edge(uuid.UUID(int=1), uuid.UUID(int=2), mc.Verdict.DUPLICATE)]
    r = mc.consolidate(refs, edges)
    assert _status(r, 2) == mc.STATUS_ACTIVE
    assert _status(r, 1) == mc.STATUS_DUPLICATE
    dup = next(t for t in r.transitions if t.registry_id == uuid.UUID(int=1))
    assert dup.duplicate_of == uuid.UUID(int=2)


def test_existing_untouched_memory_is_not_transitioned():
    """edge가 없는 기존 active는 건드리지 않는다 — 매번 다시 쓰면 revision이 튄다."""
    r = mc.consolidate([_ref(1), _ref(9, is_new=False)], [])
    assert _status(r, 9) is None


# ── validator ────────────────────────────────────────────────
def test_unknown_registry_id_makes_component_ambiguous():
    """classifier가 만들어낸 id — 일부만 적용하지 않는다."""
    refs = [_ref(1), _ref(2, is_new=False)]
    edges = [
        mc.Edge(uuid.UUID(int=1), uuid.UUID(int=2), mc.Verdict.SUPERSEDES),
        mc.Edge(uuid.UUID(int=1), uuid.UUID(int=99), mc.Verdict.SUPERSEDES),  # 없는 id
    ]
    r = mc.consolidate(refs, edges)
    assert "unknown_registry_id" in r.rejected_reasons
    assert _status(r, 1) == mc.STATUS_AMBIGUOUS
    assert _status(r, 2) == mc.STATUS_AMBIGUOUS


def test_cycle_makes_component_ambiguous():
    """A가 B를, B가 A를 대체할 수는 없다."""
    refs = [_ref(1), _ref(2)]
    edges = [
        mc.Edge(uuid.UUID(int=1), uuid.UUID(int=2), mc.Verdict.SUPERSEDES),
        mc.Edge(uuid.UUID(int=2), uuid.UUID(int=1), mc.Verdict.SUPERSEDES),
    ]
    r = mc.consolidate(refs, edges)
    assert "cycle" in r.rejected_reasons
    assert _status(r, 1) == mc.STATUS_AMBIGUOUS


def test_two_new_superseding_same_old_is_not_a_conflict():
    """여러 신규가 같은 옛 기억을 정정하는 것 자체는 모순이 아니다.

    한 덩어리에서 "회사를 옮겼다"와 "새 팀에서 일한다"가 둘 다 예전 직장 기억을 정정하는 건
    자연스럽다. 예전에는 이 둘이 서로 duplicate가 아니라는 이유로 덩어리 전체를 ambiguous로
    닫았다. 덩어리 단위 추출로 바뀐 뒤 이 모양이 자주 나온다.
    """
    refs = [_ref(1), _ref(2), _ref(3, is_new=False)]
    edges = [
        mc.Edge(uuid.UUID(int=1), uuid.UUID(int=3), mc.Verdict.SUPERSEDES),
        mc.Edge(uuid.UUID(int=2), uuid.UUID(int=3), mc.Verdict.SUPERSEDES),
    ]
    r = mc.consolidate(refs, edges)
    assert "conflicting_supersede" not in r.rejected_reasons
    assert _status(r, 3) == mc.STATUS_SUPERSEDED   # 옛것은 닫히고
    assert _status(r, 1) == mc.STATUS_ACTIVE       # 신규는 둘 다 산다
    assert _status(r, 2) == mc.STATUS_ACTIVE


def test_two_new_superseding_same_old_ok_when_mutually_duplicate():
    refs = [_ref(1), _ref(2), _ref(3, is_new=False)]
    edges = [
        mc.Edge(uuid.UUID(int=1), uuid.UUID(int=3), mc.Verdict.SUPERSEDES),
        mc.Edge(uuid.UUID(int=2), uuid.UUID(int=3), mc.Verdict.SUPERSEDES),
        mc.Edge(uuid.UUID(int=1), uuid.UUID(int=2), mc.Verdict.DUPLICATE),
        mc.Edge(uuid.UUID(int=2), uuid.UUID(int=1), mc.Verdict.DUPLICATE),
    ]
    r = mc.consolidate(refs, edges)
    assert "conflicting_supersede" not in r.rejected_reasons
    assert _status(r, 3) == mc.STATUS_SUPERSEDED


def test_explicit_ambiguous_verdict_groups_component():
    refs = [_ref(1), _ref(2, is_new=False)]
    edges = [mc.Edge(uuid.UUID(int=1), uuid.UUID(int=2), mc.Verdict.AMBIGUOUS)]
    r = mc.consolidate(refs, edges)
    groups = {t.conflict_group_id for t in r.transitions}
    assert len(groups) == 1 and None not in groups
    assert r.ambiguous_components == 1


def test_self_edge_is_ignored_not_fatal():
    """동일 source turn의 자기 자신 비교는 코드가 제외한다."""
    r = mc.consolidate(
        [_ref(1)], [mc.Edge(uuid.UUID(int=1), uuid.UUID(int=1), mc.Verdict.DUPLICATE)]
    )
    assert _status(r, 1) == mc.STATUS_ACTIVE


def test_independent_components_are_decided_separately():
    """한 component의 오류가 무관한 기억까지 ambiguous로 만들지 않는다."""
    refs = [_ref(1), _ref(2), _ref(3), _ref(4)]
    edges = [
        mc.Edge(uuid.UUID(int=1), uuid.UUID(int=2), mc.Verdict.SUPERSEDES),
        mc.Edge(uuid.UUID(int=2), uuid.UUID(int=1), mc.Verdict.SUPERSEDES),  # cycle
    ]
    r = mc.consolidate(refs, edges)
    assert _status(r, 1) == mc.STATUS_AMBIGUOUS
    assert _status(r, 3) == mc.STATUS_ACTIVE  # 무관한 기억은 정상 판정
    assert _status(r, 4) == mc.STATUS_ACTIVE


def test_canonical_picks_latest_by_occurred_then_turn_then_hash():
    newer = _ref(1, turn=5, at=_T0 + timedelta(days=1), h="a")
    older = _ref(2, turn=9, at=_T0, h="z")
    assert mc._canonical([newer, older]).registry_id == newer.registry_id


def test_no_second_llm_call_on_invalid_graph():
    """invalid graph에서 재질의하지 않는다 — 계약을 코드로 고정."""
    import ast
    import inspect

    # docstring은 '무엇을 하지 않는지' 설명이므로 제외하고 **호출 노드**만 본다.
    tree = ast.parse(inspect.getsource(mc))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
    for banned in ("generate", "generate_step", "classify", "acompletion"):
        assert banned not in called, f"consolidation이 {banned}를 호출한다"
    # import 자체도 없어야 한다(지연 import로 우회하는 것까지 차단).
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    } | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any("llm" in m for m in imported)


# ── 애매함이 번지지 않아야 한다 (2026-08-08 감사) ────────────
#
# 예전에는 edge 하나가 ambiguous면 연결된 component 전체를 닫았다. 24건이 줄줄이 이어져
# 한 덩어리가 되므로, 모델이 한 쌍만 "확신 없음"이라고 해도 멀쩡한 사실까지 전부
# "서로 어긋나는 기억"으로 프롬프트에 들어갔다.
def test_ambiguous_closes_only_the_pair_it_touches():
    refs = [_ref(1), _ref(2), _ref(3), _ref(4, is_new=False)]
    edges = [
        mc.Edge(uuid.UUID(int=1), uuid.UUID(int=2), mc.Verdict.AMBIGUOUS),
        mc.Edge(uuid.UUID(int=3), uuid.UUID(int=4), mc.Verdict.DUPLICATE),
    ]
    r = mc.consolidate(refs, edges)
    assert _status(r, 1) == mc.STATUS_AMBIGUOUS
    assert _status(r, 2) == mc.STATUS_AMBIGUOUS
    # 관계없는 쪽은 원래 판정대로 간다.
    assert _status(r, 3) == mc.STATUS_DUPLICATE


def test_cycle_in_one_group_does_not_poison_another():
    """순환 하나가 전혀 관계없는 다른 덩어리의 정상 판정까지 닫으면 안 된다."""
    refs = [_ref(1), _ref(2), _ref(3), _ref(4, is_new=False)]
    edges = [
        mc.Edge(uuid.UUID(int=1), uuid.UUID(int=2), mc.Verdict.SUPERSEDES),
        mc.Edge(uuid.UUID(int=2), uuid.UUID(int=1), mc.Verdict.SUPERSEDES),
        mc.Edge(uuid.UUID(int=3), uuid.UUID(int=4), mc.Verdict.SUPERSEDES),
    ]
    r = mc.consolidate(refs, edges)
    assert "cycle" in r.rejected_reasons
    assert _status(r, 1) == mc.STATUS_AMBIGUOUS
    assert _status(r, 4) == mc.STATUS_SUPERSEDED   # 다른 덩어리는 살아 있다


def test_backwards_supersede_is_downgraded_not_applied():
    """옛 기억이 새 기억을 덮으면, 방금 말한 사실이 닫히고 옛 값이 계속 회상된다."""
    old = _ref(1, is_new=False, turn=1)
    new = _ref(2, turn=9)
    # 모델이 방향을 뒤집어 냈다: 옛것(1)이 새것(2)을 대체한다고
    r = mc.consolidate([old, new], [mc.Edge(uuid.UUID(int=1), uuid.UUID(int=2),
                                            mc.Verdict.SUPERSEDES)])
    assert "supersede_backwards" in r.rejected_reasons
    assert _status(r, 2) != mc.STATUS_SUPERSEDED


def test_conflicting_verdicts_on_the_same_pair_are_not_order_dependent():
    """같은 쌍에 duplicate와 supersedes가 함께 오면 나중 것이 이기면 안 된다."""
    refs = [_ref(1), _ref(2, is_new=False)]
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    forward = mc.consolidate(refs, [mc.Edge(a, b, mc.Verdict.DUPLICATE),
                                    mc.Edge(a, b, mc.Verdict.SUPERSEDES)])
    reverse = mc.consolidate(refs, [mc.Edge(a, b, mc.Verdict.SUPERSEDES),
                                    mc.Edge(a, b, mc.Verdict.DUPLICATE)])
    assert "conflicting_verdicts" in forward.rejected_reasons
    assert _status(forward, 1) == _status(reverse, 1) == mc.STATUS_AMBIGUOUS
