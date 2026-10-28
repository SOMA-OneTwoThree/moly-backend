"""트레이닝 도전 기회 정산: 최대 5개, 5개보다 적으면 1시간마다 하나."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.training_tries import FULL, MAX_TRIES, Tries, settle, spend

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def test_no_row_is_a_full_set_without_refill_times():
    tries = settle(None, None, NOW)
    assert tries == FULL and tries.left == MAX_TRIES == 5
    assert tries.next_refill_at is None and tries.full_at is None


@pytest.mark.parametrize(
    ("left", "elapsed", "expected_left", "expected_start"),
    [
        (3, timedelta(minutes=59), 3, NOW),
        (3, H, 4, NOW + H),
        (3, H * 1.5, 4, NOW + H),
        (0, H * 4, 4, NOW + H * 4),
        (3, H * 2, 5, None),
        (0, H * 30, 5, None),
        (2, -H, 2, NOW),  # 시계가 뒤로 가도 충전하지 않는다
    ],
)
def test_settle_refills_one_per_hour_up_to_five(left, elapsed, expected_left, expected_start):
    tries = settle(left, NOW, NOW + elapsed)
    assert tries == Tries(expected_left, expected_start)


def test_refill_times_count_from_the_current_cycle():
    tries = settle(2, NOW, NOW + timedelta(minutes=30))
    assert tries.next_refill_at == NOW + H
    assert tries.full_at == NOW + H * 3


def test_spending_from_full_starts_the_refill_clock():
    assert spend(FULL, NOW) == Tries(4, NOW)


def test_spending_keeps_the_running_cycle():
    started = NOW - timedelta(minutes=20)
    assert spend(Tries(3, started), NOW) == Tries(2, started)


def test_cannot_spend_without_tries():
    with pytest.raises(ValueError):
        spend(Tries(0, NOW), NOW)


def test_spending_after_settling_matches_a_saved_row():
    # 정산만 하고 저장하지 않아도 같은 결과가 나온다(저장은 차감할 때만 한다).
    later = NOW + H * 2 + timedelta(minutes=10)
    assert settle(1, NOW, later) == settle(3, NOW + H * 2, later)
