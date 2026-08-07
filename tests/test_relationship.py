"""관계 stage — 결정적 계산과 단조성.

여기서 지키는 것:
 · 같은 event 집합은 순서와 무관하게 같은 stage를 만든다(replay 재현성)
 · 하루에 몰아 대화해도 단계가 튀지 않는다
 · 오래 안 와도 단계가 내려가지 않는다
 · 민감 지표(자기개방·일기 열람 등)가 입력에 없다
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from app.services import relationship as rel


def _turns(day: date, n: int, start: int = 0) -> list[tuple[str, date, int]]:
    return [(rel.EVENT_NORMAL_TURN, day, start + i) for i in range(n)]


def _days(n: int, turns_per_day: int) -> list[tuple[str, date, int]]:
    out: list[tuple[str, date, int]] = []
    seq = 0
    for d in range(n):
        day = date(2026, 1, 1) + timedelta(days=d)
        out.extend(_turns(day, turns_per_day, start=seq))
        seq += turns_per_day
    return out


# ─────────────────────────────────────────────────────────────
# 1. 단계 판정
# ─────────────────────────────────────────────────────────────
def test_new_until_thresholds_met():
    assert rel.compute_stage(active_days=1, qualifying_turns=100) == rel.STAGE_NEW
    assert rel.compute_stage(active_days=30, qualifying_turns=5) == rel.STAGE_NEW


def test_each_stage_needs_both_days_and_turns():
    assert rel.compute_stage(2, 6) == rel.STAGE_ACQUAINTED
    assert rel.compute_stage(7, 30) == rel.STAGE_FAMILIAR
    assert rel.compute_stage(30, 120) == rel.STAGE_CLOSE
    # 한쪽만 채우면 오르지 않는다
    assert rel.compute_stage(7, 29) == rel.STAGE_ACQUAINTED
    assert rel.compute_stage(6, 30) == rel.STAGE_ACQUAINTED


def test_stage_is_monotonic():
    assert rel.merge_stage(rel.STAGE_FAMILIAR, rel.STAGE_NEW) == rel.STAGE_FAMILIAR
    assert rel.merge_stage(rel.STAGE_NEW, rel.STAGE_FAMILIAR) == rel.STAGE_FAMILIAR
    assert rel.is_upgrade(rel.STAGE_CLOSE, rel.STAGE_FAMILIAR) is False


# ─────────────────────────────────────────────────────────────
# 2. 하루 상한 — 몰아 대화해도 단계가 튀지 않는다
# ─────────────────────────────────────────────────────────────
def test_one_day_binge_does_not_inflate_stage():
    """하루에 200턴을 해도 active_days가 1이라 new에 머문다."""
    events = _turns(date(2026, 1, 1), 200)
    c = rel.counters_from_events(events)
    assert c.successful_turns == 200          # raw 통계는 정확히 보존
    assert c.qualifying_turns == 10           # stage 계산은 하루 10턴까지만
    assert rel.stage_from_events(events) == rel.STAGE_NEW


def test_qualifying_turns_capped_per_day_not_globally():
    events = _days(3, turns_per_day=50)
    c = rel.counters_from_events(events)
    assert c.active_days == 3
    assert c.qualifying_turns == 30           # 3일 × 10
    assert c.successful_turns == 150


# ─────────────────────────────────────────────────────────────
# 3. replay 재현성
# ─────────────────────────────────────────────────────────────
def test_same_events_in_any_order_give_same_stage():
    events = _days(8, turns_per_day=4)
    baseline = rel.stage_from_events(events)
    for seed in (1, 2, 3):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        assert rel.stage_from_events(shuffled) == baseline


def test_duplicate_turn_events_do_not_double_count():
    """재시도·중복 finalize가 같은 turn을 두 번 넣어도 집계가 늘지 않는다."""
    events = _turns(date(2026, 1, 1), 3)
    assert rel.counters_from_events(events + events).successful_turns == 3


def test_normal_turn_implies_active_day_even_without_day_event():
    """active_day event가 유실돼도 그날은 active로 센다(집계 일관성)."""
    events = _turns(date(2026, 3, 3), 2)
    assert rel.counters_from_events(events).active_days == 1


def test_explicit_active_day_event_counts_without_turns():
    events = [(rel.EVENT_ACTIVE_DAY, date(2026, 3, 3), 0)]
    assert rel.counters_from_events(events).active_days == 1


# ─────────────────────────────────────────────────────────────
# 4. dedup key
# ─────────────────────────────────────────────────────────────
def test_dedup_keys_are_deterministic_and_distinct():
    assert rel.turn_dedup_key(7) == rel.turn_dedup_key(7)
    assert rel.turn_dedup_key(7) != rel.turn_dedup_key(8)
    assert rel.active_day_dedup_key(date(2026, 1, 1)) != rel.active_day_dedup_key(date(2026, 1, 2))
    # turn과 day가 같은 키 공간에서 충돌하지 않는다
    assert rel.turn_dedup_key(1) != rel.active_day_dedup_key(date(2026, 1, 1))


# ─────────────────────────────────────────────────────────────
# 5. 민감 지표 배제 — 계약 고정
# ─────────────────────────────────────────────────────────────
def test_stage_inputs_are_only_days_and_turns():
    """자기개방 깊이·일기 열람률·기억 개수가 입력에 없어야 한다.

    이 지표들은 취약한 발화와 사적 기록 소비를 보상 신호로 만든다(7.1절).
    """
    import ast
    import inspect

    # 판정 함수의 **실행 코드**만 본다 — docstring/주석은 '무엇을 제외했는지' 설명이라 제외.
    for fn in (rel.compute_stage, rel.counters_from_events):
        tree = ast.parse(inspect.getsource(fn))
        names = {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        } | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        for banned in ("diary_read", "disclosure", "memory_count", "self_disclosure", "read_rate"):
            assert banned not in names, f"{fn.__name__}이 {banned}를 참조한다"

    params = inspect.signature(rel.compute_stage).parameters
    assert list(params) == ["active_days", "qualifying_turns"]
    # event tuple은 (type, date, turn_seq) 셋뿐 — 다른 신호가 들어올 자리가 없다.
    assert rel.counters_from_events([(rel.EVENT_NORMAL_TURN, date(2026, 1, 1), 1)]).active_days == 1
