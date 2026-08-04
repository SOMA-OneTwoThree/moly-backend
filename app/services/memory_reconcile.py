"""판정(W8) — **코드가** ADD/REINFORCE/SUPERSEDE/KEEP_BOTH/IGNORE를 정한다. LLM은 제안만 한다.

순수 함수다(DB 접근 없음). 상태를 읽어오는 것과 쓰는 것은 `memory_repo`가 하고, 여기서는
"이 후보들을 어떻게 할 것인가"만 결정한다 — 그래야 규칙을 DB 없이 그대로 시험할 수 있다.

판정 순서는 고정이며 뒤집으면 프라이버시·정합성이 깨진다:

1. **hard filter가 가장 먼저**다. closure(구간)·`all`·predicate·`(version, hash)` marker에 걸리면
   IGNORE. LLM 제안은 이 판단을 절대 덮어쓰지 못한다.
2. 같은 hash가 이미 active면 REINFORCE(새 evidence가 있을 때만). 새 evidence가 없으면 retry/no-op
   이므로 IGNORE — 여기서 상태를 건드리면 프로필이 헛돈다.
3. identity `(kind, subject, predicate)`에 active가 없으면 ADD. predicate 없는 자유문 후보는 동일
   hash만 같은 사실로 보므로 2에서 안 걸렸으면 ADD다.
4. `multi`면 KEEP_BOTH. `single`이면 **evidence의 최대 watermark가 기존보다 클 때만** SUPERSEDE고,
   작으면 오래된 관찰이라 IGNORE, **같으면 어느 쪽도 publish하지 않고 실패**시킨다(경보).
   동률을 LLM 제안으로 깨지 않는다 — 그 순간 어느 쪽이 최신인지 알 수 없다는 뜻이다.

terminal(forgotten/superseded)은 되살리지 않는다. 같은 내용이 다시 관찰돼도 marker/closure를 통과한
**새 행**을 만들거나 IGNORE할 뿐이라, 이 모듈은 기존 행을 active로 바꾸는 판정을 아예 만들지 않는다.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from app.services import memory_norm, memory_registry, slack_notify
from app.services.memory_candidates import (
    ACTION_ADD,
    ACTION_IGNORE,
    ACTION_KEEP_BOTH,
    ACTION_REINFORCE,
    ACTION_SUPERSEDE,
    Candidate,
)
from app.services.memory_norm import NormalizedFact

_log = logging.getLogger("moly-worker")

# forget marker scope(= DB CHECK 값).
SCOPE_FACT = "fact"
SCOPE_PREDICATE = "predicate"
SCOPE_ALL = "all"

# job finalize 사유 코드(계약) — 부분 publish 대신 이 코드로 끝낸다.
RESULT_SOURCE_RANGE_CLOSED = "source_range_closed"


class ReconcileError(Exception):
    """판정 자체가 성립하지 않는 상태 — publish 없이 job 실패."""


class ReconcileConflictError(ReconcileError):
    """SUPERSEDE 동률 충돌 등 최신성을 정할 수 없는 경우. **어느 쪽도 publish하지 않는다.**"""


class SourceRangeClosedError(ReconcileError):
    """구간이 forget closure와 겹친다 — 부분 publish 금지, 전체를 이 사유로 끝낸다."""


# ─────────────────────────────────────────────────────────────
# 입력 형태 — repo가 DB에서 만들어 넣는다.
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ExistingFact:
    """비교 대상 active fact. `normalization_version`이 행마다 다를 수 있다(구 version 혼재)."""

    id: uuid.UUID
    kind: str
    subject: str | None
    predicate: str | None
    content_hash: str
    normalization_version: str
    confidence: float
    evidence_message_ids: frozenset[int]
    max_source_watermark: int | None

    @property
    def identity_key(self) -> tuple[str, str | None, str | None]:
        return (self.kind, self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class ForgetMarker:
    scope: str
    predicate: str | None = None
    normalized_hash: str | None = None
    normalization_version: str | None = None
    cut_watermark: int = 0
    future_learning: str = "block"


@dataclass(frozen=True, slots=True)
class Closure:
    from_watermark: int
    through_watermark: int


@dataclass(frozen=True, slots=True)
class Decision:
    """후보 1건의 최종 판정. `normalized`는 **현재 version** 기준이며 저장 값도 이걸 쓴다."""

    action: str
    candidate: Candidate
    normalized: NormalizedFact
    reason: str
    target_fact_ids: tuple[uuid.UUID, ...] = ()
    confidence: float = 0.0
    new_evidence_message_ids: tuple[int, ...] = ()

    @property
    def changes_state(self) -> bool:
        """IGNORE만이 아무 상태도 바꾸지 않는다(= revision을 올리지 않는다)."""
        return self.action != ACTION_IGNORE


# ─────────────────────────────────────────────────────────────
# 구간 guard — 부분 publish 금지.
# ─────────────────────────────────────────────────────────────
def assert_source_range_open(
    closures: Iterable[Closure], from_watermark: int, through_watermark: int
) -> None:
    """구간 안 watermark가 closure와 **하나라도** 겹치면 전체를 실패시킨다.

    겹치는 부분만 빼고 나머지를 publish하면, 잊은 대화가 남긴 사실이 이웃 turn의 evidence로
    되살아난다. 열린 source는 호출측이 새 generation job으로 다시 묶는다.
    """
    for c in closures:
        if from_watermark <= c.through_watermark and c.from_watermark <= through_watermark:
            raise SourceRangeClosedError(
                f"소스 구간 {from_watermark}~{through_watermark}가 closure "
                f"{c.from_watermark}~{c.through_watermark}와 겹친다"
            )


# ─────────────────────────────────────────────────────────────
# 판정
# ─────────────────────────────────────────────────────────────
def _needed_versions(markers: Sequence[ForgetMarker], existing: Sequence[ExistingFact]) -> set[str]:
    """후보 hash를 계산해야 하는 전 version. 현재 + marker들 + 기존 fact들.

    marker version을 빠뜨리면 잊은 사실이 다른 version으로 되살아나므로 반드시 전부 포함한다.
    """
    versions = {memory_norm.CURRENT_NORMALIZATION_VERSION}
    versions |= {m.normalization_version for m in markers if m.normalization_version}
    versions |= {f.normalization_version for f in existing}
    return versions


def _dedup(candidates: Sequence[Candidate]) -> list[Candidate]:
    """같은 배치 안 동일 hash 후보 병합 — evidence 합집합, confidence/importance는 최대값.

    같은 턴에서 같은 사실을 두 번 뽑는 일이 흔하다. 병합하지 않으면 하나가 ADD, 다른 하나가
    REINFORCE로 갈려 근거가 쪼개진다.
    """
    merged: dict[str, Candidate] = {}
    order: list[str] = []
    for c in candidates:
        key = memory_norm.content_hash(c.fields())
        prev = merged.get(key)
        if prev is None:
            merged[key] = c
            order.append(key)
            continue
        extra = tuple(i for i in c.evidence_message_ids if i not in prev.evidence_message_ids)
        merged[key] = replace(
            prev,
            evidence_message_ids=prev.evidence_message_ids + extra,
            confidence=max(prev.confidence, c.confidence),
            importance=max(prev.importance, c.importance),
        )
    return [merged[k] for k in order]


def _blocked_by_marker(
    markers: Sequence[ForgetMarker],
    predicate: str | None,
    hashes: Mapping[str, str],
    candidate_watermark: int,
) -> str | None:
    """hard filter. 걸리면 사유 문자열, 아니면 None."""
    for m in markers:
        applies_to_new = m.future_learning == "block" or candidate_watermark <= m.cut_watermark
        if not applies_to_new:
            continue
        if m.scope == SCOPE_ALL:
            return "forget_all"
        if m.scope == SCOPE_PREDICATE and predicate is not None and m.predicate == predicate:
            return f"forget_predicate:{predicate}"
        if m.scope == SCOPE_FACT and m.normalization_version and m.normalized_hash:
            # marker의 version으로 다시 계산한 후보 hash와 비교한다(현재 version만 보면 우회된다).
            if hashes.get(m.normalization_version) == m.normalized_hash:
                return "forget_fact"
    return None


def _candidate_watermark(candidate: Candidate, watermarks: Mapping[int, int]) -> int:
    try:
        return max(watermarks[mid] for mid in candidate.evidence_message_ids)
    except KeyError as e:
        raise ReconcileError(
            f"evidence 메시지의 watermark를 찾을 수 없다: message_id={e.args[0]}"
        ) from e


def decide(
    candidates: Sequence[Candidate],
    *,
    existing: Sequence[ExistingFact],
    markers: Sequence[ForgetMarker],
    watermarks: Mapping[int, int],
) -> list[Decision]:
    """후보들의 판정 목록. 미지원 normalization version·동률 충돌은 raise(부분 publish 금지).

    `watermarks` = evidence message_id → source_watermark(turn 연결표에서 온다).
    """
    versions = _needed_versions(markers, existing)
    decisions: list[Decision] = []

    for candidate in _dedup(candidates):
        fields = candidate.fields()
        # 미지원 version이 하나라도 있으면 여기서 raise된다 → job 실패·경보(무음 우회 차단).
        hashes = memory_norm.hashes_for_versions(fields, versions)
        normalized = memory_norm.normalize(fields)
        candidate_wm = _candidate_watermark(candidate, watermarks)

        # 1. hard filter — 모든 LLM 제안보다 먼저.
        blocked = _blocked_by_marker(markers, normalized.predicate, hashes, candidate_wm)
        if blocked is not None:
            decisions.append(_ignore(candidate, normalized, blocked))
            continue

        # 2. 같은 hash의 active fact = 같은 사실 → 새 evidence가 있을 때만 REINFORCE.
        same = [f for f in existing if hashes.get(f.normalization_version) == f.content_hash]
        if same:
            target = min(same, key=lambda f: str(f.id))  # 결정적 선택(중복 hash는 이례)
            fresh = tuple(
                i for i in candidate.evidence_message_ids if i not in target.evidence_message_ids
            )
            if not fresh:
                decisions.append(_ignore(candidate, normalized, "no_new_evidence"))
                continue
            decisions.append(
                Decision(
                    action=ACTION_REINFORCE,
                    candidate=candidate,
                    normalized=normalized,
                    reason="same_hash",
                    target_fact_ids=(target.id,),
                    confidence=max(target.confidence, candidate.confidence),
                    new_evidence_message_ids=fresh,
                )
            )
            continue

        # 3. identity 비교. predicate 없는 자유문 후보는 동일 hash만 같은 사실이라 여기서 ADD다.
        if normalized.predicate is None:
            decisions.append(_add(candidate, normalized, "no_predicate_identity"))
            continue
        rivals = [f for f in existing if f.identity_key == normalized.identity_key]
        if not rivals:
            decisions.append(_add(candidate, normalized, "new_identity"))
            continue

        # 4. cardinality — multi는 공존, single은 최신성 비교.
        if not memory_registry.is_single(normalized.predicate):
            decisions.append(
                Decision(
                    action=ACTION_KEEP_BOTH,
                    candidate=candidate,
                    normalized=normalized,
                    reason="multi_cardinality",
                    confidence=candidate.confidence,
                    new_evidence_message_ids=candidate.evidence_message_ids,
                )
            )
            continue

        known = [f for f in rivals if f.max_source_watermark is not None]
        if len(known) != len(rivals):
            # 근거 watermark를 모르는 active single fact — 최신성을 정할 수 없으니 실패시킨다.
            raise ReconcileConflictError(
                f"기존 fact의 source watermark를 알 수 없다: predicate={normalized.predicate!r}"
            )
        existing_wm = max(f.max_source_watermark or 0 for f in known)
        if candidate_wm < existing_wm:
            decisions.append(_ignore(candidate, normalized, "older_observation"))
            continue
        if candidate_wm == existing_wm:
            raise ReconcileConflictError(
                f"같은 watermark({candidate_wm})에서 내용이 충돌한다: "
                f"predicate={normalized.predicate!r} — 어느 쪽도 publish하지 않는다"
            )
        decisions.append(
            Decision(
                action=ACTION_SUPERSEDE,
                candidate=candidate,
                normalized=normalized,
                reason="newer_single_value",
                target_fact_ids=tuple(f.id for f in rivals),
                confidence=candidate.confidence,
                new_evidence_message_ids=candidate.evidence_message_ids,
            )
        )

    return decisions


def _add(candidate: Candidate, normalized: NormalizedFact, reason: str) -> Decision:
    return Decision(
        action=ACTION_ADD,
        candidate=candidate,
        normalized=normalized,
        reason=reason,
        confidence=candidate.confidence,
        new_evidence_message_ids=candidate.evidence_message_ids,
    )


def _ignore(candidate: Candidate, normalized: NormalizedFact, reason: str) -> Decision:
    return Decision(action=ACTION_IGNORE, candidate=candidate, normalized=normalized, reason=reason)


async def alert_failure(*, user_id: object, error_code: str, detail: str) -> None:
    """reconcile 실패 경보 — PII 없이 사유만. 전송 실패가 잡 상태를 되돌리지 않는다(잡 규약과 동일)."""
    _log.error("기억 reconcile 실패 — error_code=%s detail=%s", error_code, detail)
    try:
        await slack_notify.alert(
            f"🧠 [기억 reconcile 실패] code={error_code} {detail}",
            dedup_key=f"memory_reconcile:{error_code}",
        )
    except Exception as e:  # noqa: BLE001  # 경보 실패가 job 상태를 되돌리면 안 된다
        _log.warning("기억 reconcile 경보 전송 실패(무시): %r", e)
