"""mem0 consolidation — classifier 판정을 검증하고 registry 전이로 바꾼다.

기억 재설계(docs/ARCHITECTURE-capi.md 5.3절).

mem0는 candidate-add-only라 과거와 현재의 상반된 기억이 함께 남는다. **어느 쪽이 현재인지는
여기서 판정한다.** 검색은 `active|ambiguous`만 통과시키므로, provider delete가 늦어도 과거 값이
프롬프트로 다시 들어오지 않는다.

**validator가 이 모듈의 핵심이다.** classifier(LLM)는 틀릴 수 있고, 틀린 그래프를 일부만 적용하면
기억이 조용히 사라지거나 되살아난다. 그래서:
 · 존재하지 않는 registry id 참조 → 그 component 전체 ambiguous
 · cycle → **그 component 안에서만** 판정한다. 예전에는 그래프 전체를 보고 관계없는 덩어리의
   정상 판정까지 닫았다
 · 같은 쌍에 서로 다른 판정 → 그 쌍만 ambiguous. 예전에는 나중에 온 edge가 조용히 이겨서
   같은 입력이라도 순서에 따라 결과가 달라졌다
 · 옛 기억이 새 기억을 대체한다는 판정 → 그 쌍만 ambiguous. 그대로 적용하면 방금 말한
   사실이 닫히고 옛 값이 계속 회상된다
 · **invalid graph 때문에 두 번째 LLM 호출을 추가하지 않는다.** 보수적으로 ambiguous로 닫는다.

**애매함은 걸린 쌍만 닫는다.** 예전에는 edge 하나가 ambiguous면 연결된 component 전체를 닫았다.
덩어리 단위 추출로 24건이 줄줄이 이어지므로, 한 쌍만 확신이 없어도 멀쩡한 사실까지 전부
"어긋나는 기억"으로 프롬프트에 들어갔다.

여러 신규가 같은 옛 기억을 정정하는 것은 모순이 아니다 — 옛것을 닫고 신규는 전부 남긴다.

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
# 판정 자체를 못 한 기억. 회상은 active·ambiguous만 읽으므로 노출되지 않는다.
# 미판정(`pending`)으로 두면 영원히 남아 운영 전환 관문을 막는다 — 종결 상태로 닫는다.
STATUS_EXCLUDED = "excluded"

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
    """검증된 전이 목록.

    **애매함은 걸린 쌍만 닫는다.** 예전에는 edge 하나가 ambiguous면 연결된 component 전체를
    닫았다. 24건이 줄줄이 이어져 한 덩어리가 되므로, 모델이 한 쌍만 "확신 없음"이라고 해도
    멀쩡한 사실까지 전부 "어긋나는 기억"으로 프롬프트에 들어갔다.
    """
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

    # 2. 같은 쌍에 서로 다른 판정이 오면 그 쌍은 믿을 수 없다.
    #    예전에는 나중에 온 edge가 앞의 결정을 조용히 덮어써서, 같은 입력이라도 edge 순서가
    #    바뀌면 결과가 달라졌다.
    verdicts_by_pair: dict[frozenset, set[Verdict]] = defaultdict(set)
    for e in valid_edges:
        verdicts_by_pair[frozenset((e.subject, e.target))].add(e.verdict)
    conflicting = {p for p, vs in verdicts_by_pair.items() if len(vs) > 1}
    if conflicting:
        result.rejected_reasons.append("conflicting_verdicts")

    # 3. 대체 방향 검사 — subject가 target보다 나중이어야 한다.
    #    모델이 방향을 뒤집으면 방금 들어온 최신 사실이 닫히고 옛 사실이 살아남는다.
    normalized: list[Edge] = []
    seen_pairs: set[tuple] = set()
    backwards = False
    for e in valid_edges:
        pair = frozenset((e.subject, e.target))
        verdict = e.verdict
        if pair in conflicting:
            verdict = Verdict.AMBIGUOUS
        elif verdict is Verdict.SUPERSEDES and _is_older(by_id[e.subject], by_id[e.target]):
            backwards = True
            verdict = Verdict.AMBIGUOUS
        key = (e.subject, e.target, verdict)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        normalized.append(Edge(subject=e.subject, target=e.target, verdict=verdict))
    if backwards:
        result.rejected_reasons.append("supersede_backwards")

    edges_by_component: dict[int, list[Edge]] = defaultdict(list)
    comps = _components(known, normalized)
    index_of = {n: i for i, comp in enumerate(comps) for n in comp}
    for e in normalized:
        edges_by_component[index_of[e.subject]].append(e)

    for i, comp in enumerate(comps):
        comp_edges = edges_by_component.get(i, [])
        # cycle은 **그 component 안에서만** 본다. 예전에는 그래프 전체에 대해 한 번 보고,
        # 관계없는 다른 덩어리의 정상 판정까지 전부 닫았다.
        if _has_cycle(comp_edges):
            result.rejected_reasons.append("cycle")
            _mark_ambiguous(result, comp)
            continue
        if bad_ids and comp_edges:
            _mark_ambiguous(result, comp)
            continue
        _apply_component(result, comp, comp_edges, by_id)

    return result


def _is_older(subject: MemoryRef, target: MemoryRef) -> bool:
    """subject가 target보다 앞선 기억인가. 그러면 subject가 target을 대체할 수 없다."""
    return (subject.source_occurred_at, subject.source_turn_seq) < (
        target.source_occurred_at, target.source_turn_seq
    )


def _mark_ambiguous(result: Result, comp: set[uuid.UUID]) -> None:
    group = uuid.uuid4()
    for n in sorted(comp, key=str):
        result.transitions.append(
            Transition(registry_id=n, semantic_status=STATUS_AMBIGUOUS, conflict_group_id=group)
        )
    result.ambiguous_components += 1


# 한 번 닫힌 기억을 다른 edge가 다시 열지 못하게 한다. edge 순서에 결과가 달리지 않는다.
_CLOSED = {STATUS_DUPLICATE, STATUS_SUPERSEDED, STATUS_AMBIGUOUS}


def _keep_open(decided: dict[uuid.UUID, Transition], node: uuid.UUID) -> None:
    """아직 아무 판정도 안 받은 기억만 active로 둔다. 닫힌 것을 되살리지 않는다."""
    if node not in decided:
        decided[node] = Transition(node, STATUS_ACTIVE)


def _close(decided: dict[uuid.UUID, Transition], transition: Transition) -> None:
    """닫는 판정은 active를 이긴다. 이미 닫혀 있으면 먼저 온 것을 지킨다."""
    prev = decided.get(transition.registry_id)
    if prev is None or prev.semantic_status not in _CLOSED:
        decided[transition.registry_id] = transition


def _apply_component(
    result: Result,
    comp: set[uuid.UUID],
    edges: list[Edge],
    by_id: dict[uuid.UUID, MemoryRef],
) -> None:
    # 애매하다고 나온 **그 쌍만** 닫는다. 나머지는 원래 판정대로 간다.
    #
    # 예전에는 component 전체를 닫았다. 24건이 edge로 줄줄이 이어져 하나의 큰 덩어리가 되므로,
    # 모델이 한 쌍만 "확신 없음"이라고 해도 전부가 "서로 어긋나는 기억"으로 프롬프트에 들어갔다.
    # 판정 프롬프트가 "확신이 없으면 ambiguous"를 권장하고 있어 이 경로는 자주 밟힌다.
    uncertain = {
        n for e in edges if e.verdict is Verdict.AMBIGUOUS for n in (e.subject, e.target)
    }
    decided: dict[uuid.UUID, Transition] = {}
    if uncertain:
        group = uuid.uuid4()
        for n in uncertain:
            decided[n] = Transition(
                registry_id=n, semantic_status=STATUS_AMBIGUOUS, conflict_group_id=group
            )
        result.ambiguous_components += 1

    for e in edges:
        # 애매한 쪽에 걸린 기억은 이미 닫혔다 — 다른 판정으로 덮어쓰지 않는다.
        if e.subject in uncertain or e.target in uncertain:
            continue
        if e.verdict is Verdict.SUPERSEDES:
            _keep_open(decided, e.subject)
            _close(decided, Transition(
                e.target, STATUS_SUPERSEDED,
                provider_delete_state=DELETE_PENDING, superseded_by=e.subject,
            ))
        elif e.verdict is Verdict.DUPLICATE:
            # canonical은 유지하고 나머지를 duplicate로 닫는다.
            pair = [by_id[e.subject], by_id[e.target]]
            canonical = _canonical([p for p in pair if not p.is_new] or pair)
            loser = e.target if canonical.registry_id == e.subject else e.subject
            _keep_open(decided, canonical.registry_id)
            _close(decided, Transition(
                loser, STATUS_DUPLICATE,
                provider_delete_state=DELETE_PENDING, duplicate_of=canonical.registry_id,
            ))

    for n in sorted(comp, key=str):
        # edge가 없는 신규는 independent다. 기존 active는 건드리지 않는다.
        if n not in decided:
            if by_id[n].is_new:
                decided[n] = Transition(n, STATUS_ACTIVE)
            else:
                continue
        result.transitions.append(decided[n])
