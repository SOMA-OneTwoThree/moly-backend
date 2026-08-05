"""단계 예산 — finalize 시간을 남겨 lease를 지킨다.

하나의 handler timeout을 외부 호출에 통째로 넘기면 첫 호출이 예산을 다 쓰고 finalize할 시간이
없어진다. lease를 잃은 채 확정하면 남의 실행 결과를 덮어쓴다.
"""
from __future__ import annotations

import time

import pytest

from app.services import mem0_budget as mb


def test_stage_sum_must_fit_handler_timeout():
    """배분 합이 timeout을 넘으면 애초에 만들 수 없다."""
    with pytest.raises(ValueError):
        mb.StageBudget(total_s=10.0, stages={"a": 6.0, "b": 6.0})


def test_documented_budgets_fit():
    for factory in (
        mb.memory_ingest_budget, mb.memory_consolidation_budget, mb.context_summary_budget
    ):
        b = factory()
        assert sum(b.stages.values()) <= b.total_s


def test_each_stage_gets_its_allocation_when_on_time():
    b = mb.memory_ingest_budget()
    assert b.timeout_for("extract") == pytest.approx(15.0, abs=0.5)
    assert b.timeout_for("upsert") == pytest.approx(12.0, abs=0.5)


def test_reserve_after_protects_later_stages():
    """extract 뒤에는 embed+upsert+finalize+wrapper가 남아야 한다."""
    b = mb.memory_ingest_budget()
    assert b.reserve_after("extract") == pytest.approx(5 + 12 + 5 + 3)
    assert b.reserve_after("wrapper") == 0


def test_slow_earlier_stage_shrinks_later_stage_not_finalize():
    """앞이 늦어지면 뒤 단계가 줄지, finalize 예산을 잡아먹지 않는다."""
    b = mb.StageBudget(total_s=1.0, stages={"work": 0.6, "finalize": 0.3, "wrapper": 0.1})
    time.sleep(0.5)
    t = b.timeout_for("work")
    # 배정 0.6이지만 남은 ~0.5에서 예약분 0.4를 뺀 ~0.1만 쓸 수 있다.
    assert t < 0.6                  # 배정보다 줄었다
    assert 0 < t <= 0.15            # finalize+wrapper 예약분(0.4)을 잡아먹지 않았다


def test_refuses_to_start_when_reserve_cannot_be_met():
    """시작해놓고 중간에 끊기면 provider엔 반영됐는데 DB엔 없는 구간이 생긴다."""
    b = mb.StageBudget(total_s=0.5, stages={"work": 0.2, "finalize": 0.2, "wrapper": 0.1})
    time.sleep(0.45)
    with pytest.raises(mb.BudgetExceeded) as e:
        b.timeout_for("work")
    assert e.value.stage == "work"


def test_unknown_stage_is_rejected():
    with pytest.raises(KeyError):
        mb.memory_ingest_budget().timeout_for("nope")


def test_remaining_never_negative():
    b = mb.StageBudget(total_s=0.05, stages={"a": 0.01})
    time.sleep(0.1)
    assert b.remaining == 0.0
