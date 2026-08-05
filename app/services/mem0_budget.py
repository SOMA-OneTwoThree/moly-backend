"""외부 호출 단계 예산 — 하나의 monotonic deadline을 단계별로 쪼갠다.

기억 재설계(docs/capi-memory-ARCHITECTURE.md 13.2절).

**하나의 handler timeout을 외부 호출 전체에 그대로 넘기지 않는다.** 그러면 첫 호출이 예산을 다
쓰고 finalize할 시간이 없어 lease를 잃는다. 잃은 lease로 확정하면 남의 실행 결과를 덮어쓴다.

`memory_ingest`(40초)의 배분:
    extractor 15s · batch embedding 5s · insert_many 12s · finalize 5s · wrapper/cancel 3s

핵심 규칙: **남은 시간이 다음 단계 deadline보다 작으면 그 호출을 시작하지 않고 retry한다.**
시작해놓고 중간에 끊기면 provider에는 반영됐는데 우리 DB에는 없는 구간이 생긴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


class BudgetExceeded(Exception):
    """남은 예산이 부족해 단계를 시작하지 않았다 — 재시도 대상이다."""

    def __init__(self, stage: str, remaining: float, needed: float) -> None:
        super().__init__(f"{stage}: 남은 {remaining:.2f}s < 필요 {needed:.2f}s")
        self.stage = stage
        self.remaining = remaining
        self.needed = needed


# queue별 단계 예산(초). 합이 handler timeout 이하여야 한다.
MEMORY_INGEST_STAGES: dict[str, float] = {
    "extract": 15.0,
    "embed": 5.0,
    "upsert": 12.0,
    "finalize": 5.0,
    "wrapper": 3.0,
}

MEMORY_CONSOLIDATION_STAGES: dict[str, float] = {
    "search": 6.0,
    "classify": 24.0,
    "validate": 4.0,
    "finalize": 5.0,
    "wrapper": 6.0,
}

CONTEXT_SUMMARY_STAGES: dict[str, float] = {
    "model": 55.0,
    "verify": 5.0,
    "finalize": 5.0,
    "wrapper": 10.0,
}


@dataclass
class StageBudget:
    """단일 monotonic deadline 안에서 단계별 시간을 배분한다."""

    total_s: float
    stages: dict[str, float]
    _started: float = 0.0

    def __post_init__(self) -> None:
        allocated = sum(self.stages.values())
        if allocated > self.total_s:
            raise ValueError(
                f"단계 합계 {allocated}s가 handler timeout {self.total_s}s를 넘는다"
            )
        self._started = monotonic()

    @property
    def elapsed(self) -> float:
        return monotonic() - self._started

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_s - self.elapsed)

    def reserve_after(self, stage: str) -> float:
        """이 단계 뒤에 반드시 남겨야 하는 시간(finalize·wrapper 등)."""
        keys = list(self.stages)
        idx = keys.index(stage)
        return sum(self.stages[k] for k in keys[idx + 1:])

    def timeout_for(self, stage: str) -> float:
        """이 단계에 줄 timeout. 시작할 수 없으면 `BudgetExceeded`.

        배정값과 "남은 시간 - 이후 예약분" 중 **작은 쪽**을 준다. 앞 단계가 늦어졌으면
        뒤 단계가 그만큼 줄어들지, finalize 예산을 잡아먹지 않는다.
        """
        if stage not in self.stages:
            raise KeyError(f"모르는 단계: {stage!r}")
        reserve = self.reserve_after(stage)
        usable = self.remaining - reserve
        if usable <= 0:
            raise BudgetExceeded(stage, self.remaining, reserve + self.stages[stage])
        return min(self.stages[stage], usable)


def memory_ingest_budget(total_s: float = 40.0) -> StageBudget:
    return StageBudget(total_s=total_s, stages=dict(MEMORY_INGEST_STAGES))


def memory_consolidation_budget(total_s: float = 45.0) -> StageBudget:
    return StageBudget(total_s=total_s, stages=dict(MEMORY_CONSOLIDATION_STAGES))


def context_summary_budget(total_s: float = 75.0) -> StageBudget:
    return StageBudget(total_s=total_s, stages=dict(CONTEXT_SUMMARY_STAGES))
