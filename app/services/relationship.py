"""관계 상태 — event에서 stage를 **결정적으로** 계산한다.

기억 재설계 5단계(docs/capi-memory-ARCHITECTURE.md 7절).

불변식:
 1. stage는 LLM 서술이 아니라 event 집합의 함수다. "가까워진 것 같다"는 말로 오르지 않는다.
 2. **같은 rule version + 같은 event 집합 → 항상 같은 stage.** replay로 재현 가능해야 한다.
 3. stage는 **단조 증가**한다. 오래 안 왔다고 내리지 않는다 — 관계를 벌점으로 관리하지 않는다.
 4. 점수 입력에 민감 지표를 쓰지 않는다. 자기개방 깊이·건강/가족 발화량·기억 개수·일기 열람률은
    **제외**한다. 그런 걸 보상 신호로 만들면 취약한 발화와 사적 기록 소비를 유도하게 된다.
 5. 하루에 몰아서 대화해도 단계가 튀지 않게 stage용 turn은 **하루 10턴까지만** 누적한다.
    raw 통계(`successful_turns`)는 별도로 정확히 보존한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# rule version — threshold를 바꾸면 올리고 event에서 전량 재계산한다(제자리 수정 금지).
STAGE_RULE_VERSION = "relationship-v1"

STAGE_NEW = "new"
STAGE_ACQUAINTED = "acquainted"
STAGE_FAMILIAR = "familiar"
STAGE_CLOSE = "close"

# 낮은 것부터. 단조 증가 판정에 쓴다.
STAGE_ORDER: tuple[str, ...] = (STAGE_NEW, STAGE_ACQUAINTED, STAGE_FAMILIAR, STAGE_CLOSE)

# event_type — 이 둘만 stage 계산에 들어간다.
EVENT_NORMAL_TURN = "normal_turn_committed"
EVENT_ACTIVE_DAY = "active_day_started"

# stage용 turn은 하루 최대 이만큼만 센다.
MAX_QUALIFYING_TURNS_PER_DAY = 10

# (stage, 필요 active_days, 필요 qualifying_turns) — 위에서부터 먼저 만족하는 것.
_THRESHOLDS: tuple[tuple[str, int, int], ...] = (
    (STAGE_CLOSE, 30, 120),
    (STAGE_FAMILIAR, 7, 30),
    (STAGE_ACQUAINTED, 2, 6),
)


@dataclass(frozen=True, slots=True)
class RelationshipCounters:
    active_days: int
    qualifying_turns: int
    successful_turns: int


def compute_stage(active_days: int, qualifying_turns: int) -> str:
    """두 값만으로 결정되는 순수 함수. 같은 입력은 항상 같은 출력이다."""
    for stage, need_days, need_turns in _THRESHOLDS:
        if active_days >= need_days and qualifying_turns >= need_turns:
            return stage
    return STAGE_NEW


def is_upgrade(current: str, candidate: str) -> bool:
    """stage는 단조 증가한다 — 내려가는 전이는 적용하지 않는다."""
    return STAGE_ORDER.index(candidate) > STAGE_ORDER.index(current)


def merge_stage(current: str, candidate: str) -> str:
    """rule 변경 후 재계산에도 `max(기존, 새 계산)`을 쓴다(7.2절)."""
    return candidate if is_upgrade(current, candidate) else current


def turn_dedup_key(turn_seq: int) -> str:
    """같은 turn을 두 번 집계하지 않는다. 재시도·중복 finalize에 안전하다."""
    return f"turn:{turn_seq}"


def active_day_dedup_key(activity_date: date) -> str:
    """하루의 첫 성공 normal turn에서만 한 번 기록된다."""
    return f"day:{activity_date.isoformat()}"


def counters_from_events(events: list[tuple[str, date, int]]) -> RelationshipCounters:
    """event 목록 → counter. backfill과 런타임이 **같은 함수**를 쓴다.

    `events` = (event_type, activity_date, turn_seq). 순서에 의존하지 않는다 —
    replay 순서가 달라도 같은 결과가 나와야 한다(불변식 2).
    """
    active_days: set[date] = set()
    turns_by_day: dict[date, set[int]] = {}
    for event_type, day, turn_seq in events:
        if event_type == EVENT_ACTIVE_DAY:
            active_days.add(day)
        elif event_type == EVENT_NORMAL_TURN:
            turns_by_day.setdefault(day, set()).add(turn_seq)
            # normal turn이 있으면 그날은 active day다(event 누락에도 일관되게).
            active_days.add(day)
    successful = sum(len(t) for t in turns_by_day.values())
    qualifying = sum(min(len(t), MAX_QUALIFYING_TURNS_PER_DAY) for t in turns_by_day.values())
    return RelationshipCounters(
        active_days=len(active_days),
        qualifying_turns=qualifying,
        successful_turns=successful,
    )


def stage_from_events(events: list[tuple[str, date, int]]) -> str:
    c = counters_from_events(events)
    return compute_stage(c.active_days, c.qualifying_turns)
