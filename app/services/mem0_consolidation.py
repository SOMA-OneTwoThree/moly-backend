"""mem0 consolidation — classifier 판정을 검증하고 registry 전이로 바꾼다.

기억 재설계(docs/ARCHITECTURE-capi.md 5.3절).

mem0는 candidate-add-only라 과거와 현재의 상반된 기억이 함께 남는다. **어느 쪽이 현재인지는
여기서 판정한다.** 검색은 `active|ambiguous`만 통과시키므로, provider delete가 늦어도 과거 값이
프롬프트로 다시 들어오지 않는다.

**validator가 이 모듈의 핵심이다.** classifier(LLM)는 틀릴 수 있고, 틀린 그래프를 일부만 적용하면
기억이 조용히 사라지거나 되살아난다. 그래서:
 · 존재하지 않는 registry id 참조 → 그 component 전체 ambiguous
 · cycle → 그 component 전체 ambiguous
 · 같은 old를 둘 이상이 supersede하는데 그 신규끼리 duplicate가 아님 → 전체 ambiguous
 · **invalid graph 때문에 두 번째 LLM 호출을 추가하지 않는다.** 보수적으로 ambiguous로 닫는다.

ambiguous는 "판정 실패"가 아니라 **정상 종결 상태**다. 양쪽을 발생 시각과 함께 보여주고
현재 상태를 단정하지 않는다.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Verdict(str, Enum):
    INDEPENDENT = "independent"
    DUPLICATE = "duplicate"
    SUPERSEDES = "supersedes"
    AMBIGUOUS = "ambiguous"


# registry semantic_status
STATUS_ACTIVE = "active"
STATUS_DUPLICATE = "duplicate"
STATUS_SUPERSEDED = "superseded"
STATUS_AMBIGUOUS = "ambiguous"

DELETE_KEPT = "kept"
DELETE_PENDING = "pending"


@dataclass(frozen=True, slots=True)
class MemoryRef:
    """판정 대상 1건. 정렬 우선순위(최신 canonical 선택)에 쓰는 좌표를 함께 든다."""

    registry_id: uuid.UUID
    source_turn_seq: int
    candidate_hash: str
    source_occurred_at: datetime
    is_new: bool


@dataclass(frozen=True, slots=True)
class Edge:
    """classifier가 낸 판정 1건. `subject`가 `target`에 대해 verdict를 갖는다."""

    subject: uuid.UUID  # 신규
    target: uuid.UUID   # 비교 대상(신규 또는 기존)
    verdict: Verdict


@dataclass(slots=True)
class Transition:
    registry_id: uuid.UUID
    semantic_status: str
    provider_delete_state: str = DELETE_KEPT
    duplicate_of: uuid.UUID | None = None
    superseded_by: uuid.UUID | None = None
    conflict_group_id: uuid.UUID | None = None


@dataclass(slots=True)
class Result:
    transitions: list[Transition] = field(default_factory=list)
    ambiguous_components: int = 0
    rejected_reasons: list[str] = field(default_factory=list)


def _canonical(refs: list[MemoryRef]) -> MemoryRef:
    """최신 canonical 선택 — `(max(source_occurred_at), source_turn_seq, candidate_hash)` 정렬."""
    return max(refs, key=lambda r: (r.source_occurred_at, r.source_turn_seq, r.candidate_hash))


def _components(nodes: set[uuid.UUID], edges: list[Edge]) -> list[set[uuid.UUID]]:
    """무방향 연결 component. 판정은 component 단위로 원자 적용된다."""
    adj: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for e in edges:
        adj[e.subject].add(e.target)
        adj[e.target].add(e.subject)
    seen: set[uuid.UUID] = set()
    out: list[set[uuid.UUID]] = []
    for n in nodes:
        if n in seen:
            continue
        stack, comp = [n], set()
        while stack:
            cur = stack.pop()
            if cur in comp:
                continue
            comp.add(cur)
            stack.extend(adj[cur] - comp)
        seen |= comp
        out.append(comp)
    return out


def _has_cycle(edges: list[Edge]) -> bool:
    """supersedes 방향 그래프의 cycle. A가 B를, B가 A를 대체할 수는 없다."""
    graph: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for e in edges:
        if e.verdict is Verdict.SUPERSEDES:
            graph[e.subject].add(e.target)
    state: dict[uuid.UUID, int] = {}

    def visit(n: uuid.UUID) -> bool:
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        for m in graph[n]:
            if visit(m):
                return True
        state[n] = 2
        return False

    return any(visit(n) for n in list(graph))


def consolidate(refs: list[MemoryRef], edges: list[Edge]) -> Result:
    """검증된 전이 목록. 위반 component는 통째로 ambiguous로 닫는다."""
    result = Result()
    by_id = {r.registry_id: r for r in refs}
    known = set(by_id)

    # 1. 존재하지 않는 id를 참조하는 edge — classifier가 만들어낸 id다.
    valid_edges, bad_ids = [], False
    for e in edges:
        if e.subject not in known or e.target not in known:
            bad_ids = True
            continue
        if e.subject == e.target:  # 자기 자신 비교는 코드가 제외한다
            continue
        valid_edges.append(e)
    if bad_ids:
        result.rejected_reasons.append("unknown_registry_id")

    cyclic = _has_cycle(valid_edges)
    if cyclic:
        result.rejected_reasons.append("cycle")

    edges_by_component: dict[int, list[Edge]] = defaultdict(list)
    comps = _components(known, valid_edges)
    index_of = {n: i for i, comp in enumerate(comps) for n in comp}
    for e in valid_edges:
        edges_by_component[index_of[e.subject]].append(e)

    for i, comp in enumerate(comps):
        comp_edges = edges_by_component.get(i, [])
        # id 오류나 cycle이 있으면 **관련 component 전체**를 보수적으로 ambiguous.
        if cyclic and any(e.verdict is Verdict.SUPERSEDES for e in comp_edges):
            _mark_ambiguous(result, comp)
            continue
        if bad_ids and comp_edges:
            _mark_ambiguous(result, comp)
            continue

        # 같은 old를 둘 이상이 supersede → 그 신규끼리 duplicate여야 한다.
        supersede_targets: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for e in comp_edges:
            if e.verdict is Verdict.SUPERSEDES:
                supersede_targets[e.target].append(e.subject)
        conflicted = False
        for _target, subjects in supersede_targets.items():
            if len(subjects) <= 1:
                continue
            pairs = {
                frozenset((e.subject, e.target))
                for e in comp_edges
                if e.verdict is Verdict.DUPLICATE
            }
            for a in subjects:
                for b in subjects:
                    if a != b and frozenset((a, b)) not in pairs:
                        conflicted = True
        if conflicted:
            result.rejected_reasons.append("conflicting_supersede")
            _mark_ambiguous(result, comp)
            continue

        _apply_component(result, comp, comp_edges, by_id)

    return result


def _mark_ambiguous(result: Result, comp: set[uuid.UUID]) -> None:
    group = uuid.uuid4()
    for n in sorted(comp, key=str):
        result.transitions.append(
            Transition(registry_id=n, semantic_status=STATUS_AMBIGUOUS, conflict_group_id=group)
        )
    result.ambiguous_components += 1


def _apply_component(
    result: Result,
    comp: set[uuid.UUID],
    edges: list[Edge],
    by_id: dict[uuid.UUID, MemoryRef],
) -> None:
    if any(e.verdict is Verdict.AMBIGUOUS for e in edges):
        _mark_ambiguous(result, comp)
        return

    decided: dict[uuid.UUID, Transition] = {}
    for e in edges:
        if e.verdict is Verdict.SUPERSEDES:
            decided[e.subject] = Transition(e.subject, STATUS_ACTIVE)
            decided[e.target] = Transition(
                e.target, STATUS_SUPERSEDED,
                provider_delete_state=DELETE_PENDING, superseded_by=e.subject,
            )
        elif e.verdict is Verdict.DUPLICATE:
            # canonical은 유지하고 나머지를 duplicate로 닫는다.
            pair = [by_id[e.subject], by_id[e.target]]
            canonical = _canonical([p for p in pair if not p.is_new] or pair)
            loser = e.target if canonical.registry_id == e.subject else e.subject
            decided.setdefault(canonical.registry_id, Transition(canonical.registry_id, STATUS_ACTIVE))
            decided[loser] = Transition(
                loser, STATUS_DUPLICATE,
                provider_delete_state=DELETE_PENDING, duplicate_of=canonical.registry_id,
            )

    for n in sorted(comp, key=str):
        # edge가 없는 신규는 independent다. 기존 active는 건드리지 않는다.
        if n not in decided:
            if by_id[n].is_new:
                decided[n] = Transition(n, STATUS_ACTIVE)
            else:
                continue
        result.transitions.append(decided[n])
