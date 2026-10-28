"""트레이닝 도전 기회 정산 — 순수 함수. 최대 5개, 5개보다 적으면 1시간마다 하나씩 다시 찬다.

`refill_from`은 지금 충전 주기의 시작 시각이다. 5개에서 하나를 쓰는 순간 시작되고, 한 시간이
지날 때마다 하나를 채우며 그만큼 앞으로 옮긴다. 다 차면 비운다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_TRIES = 5
REFILL_EVERY = timedelta(hours=1)


@dataclass(frozen=True)
class Tries:
    left: int
    refill_from: datetime | None

    @property
    def next_refill_at(self) -> datetime | None:
        return None if self.refill_from is None else self.refill_from + REFILL_EVERY

    @property
    def full_at(self) -> datetime | None:
        if self.refill_from is None:
            return None
        return self.refill_from + REFILL_EVERY * (MAX_TRIES - self.left)


FULL = Tries(MAX_TRIES, None)


def settle(left: int | None, refill_from: datetime | None, now: datetime) -> Tries:
    """저장된 값(행이 없으면 None)에 지난 시간만큼 충전을 반영한다."""
    if left is None or refill_from is None or left >= MAX_TRIES:
        return FULL
    gained = max(0, (now - refill_from) // REFILL_EVERY)
    if left + gained >= MAX_TRIES:
        return FULL
    return Tries(left + gained, refill_from + REFILL_EVERY * gained)


def spend(tries: Tries, now: datetime) -> Tries:
    """정산된 기회에서 하나를 쓴다. 다 차 있었다면 지금부터 충전을 시작한다."""
    if tries.left <= 0:
        raise ValueError("no tries left")
    if tries.left == MAX_TRIES:
        return Tries(MAX_TRIES - 1, now)
    return Tries(tries.left - 1, tries.refill_from)
